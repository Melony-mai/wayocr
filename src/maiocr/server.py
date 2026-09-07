"""MaiOCR persistent server.

The server keeps a lightweight Python process resident in RAM and
listens on a Unix socket for client requests. The OCR model is
initialised lazily on the first `recognize` request and can be
unloaded via a `release_vram` request, without terminating the
service.

Supported requests (JSON over the socket):

* ``{"op": "recognize", "image": "<path>", "lang": "<lang>"}`` ->
  ``{"text": "...", "boxes": [...]}``
* ``{"op": "status"}`` -> ``{"status": "ok", "engine_loaded": bool,
  "provider": "...", "boxes": [...]}``
* ``{"op": "release_vram"}`` -> ``{"released": true/false}``
* ``{"op": "ping"}`` -> ``{"pong": true}``
* ``{"op": "settings", ...}`` -> settings read/write

The server is intentionally quiet: log lines are written in English so
they are searchable; user-facing error messages are localised using
the request's preferred language (``lang`` field) or the system
default.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict

from maiocr import gpu, i18n, logging_utils, processor, settings as settings_mod
from maiocr.ocr import OCR
from maiocr import paths
from maiocr.preprocess import preprocess_image


def _log(msg: str) -> None:
    """Append a line to the rotating server log."""
    logger = logging_utils.get_logger()
    logger.info(msg)
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _write_status(engine_loaded: bool, provider: str) -> None:
    info = gpu.detect()
    payload = {
        "service": "maiocr",
        "engine_loaded": engine_loaded,
        "provider": provider if engine_loaded else "unloaded",
        "gpu_available": info.available,
        "gpu_device": info.device_name,
        "updated": time.time(),
    }
    try:
        paths.STATUS_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    except Exception as exc:  # pragma: no cover
        _log(f"status write failed: {exc}")


def _cleanup_socket() -> None:
    try:
        if paths.SOCKET_PATH.exists():
            paths.SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass


def _write_pid() -> None:
    try:
        paths.PID_FILE.write_text(str(os.getpid()))
    except Exception as exc:
        _log(f"pid write failed: {exc}")


def _drop_pid() -> None:
    try:
        if paths.PID_FILE.exists():
            paths.PID_FILE.unlink()
    except Exception:
        pass


def _tr(language: str, key: str, **kwargs: Any) -> str:
    """Translate ``key`` using ``language`` (falls back internally)."""
    saved = i18n.get_default_language()
    try:
        i18n.set_default_language(i18n.resolve_language(language))
        return i18n.t(key, **kwargs)
    finally:
        i18n.set_default_language(saved)


def _auto_release(ocr: OCR) -> None:
    """Release the OCR engine if the user enabled auto-release in settings."""
    s = settings_mod.get_settings()
    if s.auto_release_vram:
        try:
            ocr.release_vram()
        except Exception:
            pass


def _maybe_release_idle(ocr: OCR, last_used: float) -> None:
    """Release the engine if it has been idle for too long."""
    s = settings_mod.get_settings()
    if s.auto_release_seconds <= 0 or not ocr.loaded:
        return
    if time.time() - last_used >= s.auto_release_seconds:
        try:
            ocr.release_vram()
        except Exception:
            pass


def handle_request(conn: socket.socket, ocr: OCR) -> str:
    """Handle one request, return the op name (for the caller's bookkeeping)."""
    op_name = "unknown"
    try:
        data = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(chunk) < 65536:
                try:
                    json.loads(data.decode("utf-8"))
                    break
                except Exception:
                    continue

        if not data:
            return op_name

        try:
            request: Dict[str, Any] = json.loads(data.decode("utf-8"))
        except Exception as exc:
            conn.sendall(
                json.dumps({"error": f"bad request: {exc}"}).encode("utf-8")
            )
            return op_name

        op = request.get("op", "recognize")
        op_name = op
        lang = request.get("lang") or i18n.get_default_language()

        if op == "ping":
            conn.sendall(json.dumps({"pong": True}).encode("utf-8"))
            return op_name

        if op == "status":
            payload = {
                "status": "ok",
                "engine_loaded": ocr.loaded,
                "provider": ocr.provider,
                "gpu_available": ocr.info.available,
                "gpu_device": ocr.info.device_name,
            }
            conn.sendall(json.dumps(payload).encode("utf-8"))
            return op_name

        if op == "release_vram":
            released = ocr.release_vram()
            _write_status(engine_loaded=False, provider=ocr.provider)
            _log(f"release_vram requested -> released={released}")
            conn.sendall(json.dumps({"released": released}).encode("utf-8"))
            return op_name

        if op == "shutdown":
            conn.sendall(json.dumps({"shutdown": True}).encode("utf-8"))
            conn.close()
            os._exit(0)  # noqa: SLF001
            return op_name

        if op == "settings_get":
            s = settings_mod.get_settings(reload=True)
            conn.sendall(json.dumps({"settings": s.as_dict()}).encode("utf-8"))
            return op_name

        if op == "settings_set":
            updates = request.get("settings") or {}
            if not isinstance(updates, dict):
                conn.sendall(
                    json.dumps({"error": "settings must be an object"}).encode("utf-8")
                )
                return op_name
            s = settings_mod.get_settings(reload=True)
            s.update(**updates)
            settings_mod.save_settings(s)
            i18n.set_default_language(s.language)
            conn.sendall(
                json.dumps({"settings": s.as_dict()}).encode("utf-8")
            )
            return op_name

        if op != "recognize":
            conn.sendall(
                json.dumps({"error": f"unknown op: {op}"}).encode("utf-8")
            )
            return op_name

        image_path = request.get("image")
        if not image_path or not Path(image_path).exists():
            conn.sendall(
                json.dumps(
                    {"error": _tr(lang, "errors.image_not_found", path=image_path)}
                ).encode("utf-8")
            )
            return op_name

        start = time.perf_counter()

        try:
            processed = preprocess_image(image_path)
        except Exception as exc:
            conn.sendall(
                json.dumps(
                    {"error": _tr(lang, "errors.preprocess_failed", error=str(exc))}
                ).encode("utf-8")
            )
            return op_name

        pre_t = time.perf_counter()

        try:
            boxes = ocr.recognize(processed)
        except Exception as exc:
            _log(f"OCR error: {exc}\n{traceback.format_exc()}")
            conn.sendall(
                json.dumps({"error": _tr(lang, "errors.ocr_failed", error=str(exc))}).encode("utf-8")
            )
            return op_name

        ocr_t = time.perf_counter()

        text = processor.render(boxes)
        total_t = time.perf_counter() - start

        _log(
            f"recognize: {len(boxes.boxes)} boxes | "
            f"pre {pre_t - start:.3f}s ocr {ocr_t - pre_t:.3f}s "
            f"total {total_t:.3f}s | provider={ocr.provider}"
        )

        _write_status(engine_loaded=ocr.loaded, provider=ocr.provider)
        _auto_release(ocr)

        conn.sendall(
            json.dumps(
                {
                    "text": text,
                    "boxes": boxes.to_list(),
                    "provider": ocr.provider,
                    "preprocess_s": pre_t - start,
                    "ocr_s": ocr_t - pre_t,
                    "total_s": total_t,
                },
                ensure_ascii=False,
            ).encode("utf-8")
        )
    except Exception as exc:
        try:
            conn.sendall(
                json.dumps({"error": f"server error: {exc}"}).encode("utf-8")
            )
        except Exception:
            pass
        _log(f"unhandled error: {exc}\n{traceback.format_exc()}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return op_name


def main() -> None:
    # Configure the rotating log file before anything else
    logging_utils.configure()
    _cleanup_socket()
    _write_pid()

    def _term(_signum, _frame):  # noqa: ANN001
        _log("shutdown signal received")
        sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, _term)
        signal.signal(signal.SIGINT, _term)
    except Exception:
        pass

    s = settings_mod.get_settings()
    i18n.set_default_language(s.language)

    info = gpu.detect()
    _log(
        f"MaiOCR server starting | provider_hint={info.provider} "
        f"gpu_available={info.available} device={info.device_name} "
        f"language={s.language} "
        f"auto_release_vram={s.auto_release_vram} "
        f"auto_release_seconds={s.auto_release_seconds}"
    )

    ocr = OCR(prefer_gpu=s.prefer_gpu)
    _write_status(engine_loaded=False, provider=ocr.provider)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(paths.SOCKET_PATH))
    os.chmod(paths.SOCKET_PATH, 0o600)
    server.listen(8)
    _log(f"listening on {paths.SOCKET_PATH}")

    last_used = time.time()
    server.settimeout(1.0)  # allow periodic idle check

    try:
        while True:
            # Idle auto-release: if the engine is loaded and has been
            # idle for long enough, drop it. Reload the settings on
            # every check so the user can change the threshold at
            # runtime.
            current = settings_mod.get_settings(reload=False)
            idle_for = time.time() - last_used
            if (
                current.auto_release_seconds > 0
                and ocr.loaded
                and idle_for >= current.auto_release_seconds
            ):
                try:
                    if ocr.release_vram():
                        _log(
                            f"auto-release: engine unloaded after "
                            f"{int(idle_for)}s of idleness"
                        )
                except Exception as exc:
                    _log(f"auto-release failed: {exc}")
                last_used = time.time()

            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                op = handle_request(conn, ocr)
                # Only reset the idle timer when the engine actually
                # did OCR work; status / ping / settings requests do
                # not keep the engine "warm".
                if op == "recognize":
                    last_used = time.time()
            finally:
                pass
    finally:
        try:
            ocr.release_vram()
        except Exception:
            pass
        try:
            server.close()
        except Exception:
            pass
        _cleanup_socket()
        _drop_pid()
        _write_status(engine_loaded=False, provider="unloaded")
        _log("MaiOCR server stopped")


if __name__ == "__main__":
    main()
