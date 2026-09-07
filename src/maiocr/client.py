"""MaiOCR client: talks to the persistent server over a Unix socket."""
from __future__ import annotations

import json
import socket
from typing import Any, Dict, Optional

from maiocr import i18n, paths


def _socket_path():
    return paths.SOCKET_PATH


def _request(payload: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(str(_socket_path()))
        sock.sendall(json.dumps(payload).encode("utf-8"))
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(chunk) < 65536:
                try:
                    json.loads(data.decode("utf-8"))
                    break
                except Exception:
                    continue
        return json.loads(data.decode("utf-8"))
    finally:
        try:
            sock.close()
        except Exception:
            pass


def is_running(timeout: float = 1.0) -> bool:
    try:
        resp = _request({"op": "ping"}, timeout=timeout)
        return bool(resp.get("pong"))
    except Exception:
        return False


def status(timeout: float = 2.0) -> Optional[Dict[str, Any]]:
    try:
        return _request({"op": "status"}, timeout=timeout)
    except Exception:
        return None


def release_vram(timeout: float = 10.0) -> Optional[Dict[str, Any]]:
    try:
        return _request({"op": "release_vram"}, timeout=timeout)
    except Exception as exc:
        return {"error": str(exc)}


def recognize(
    image_path: str,
    timeout: float = 30.0,
    language: Optional[str] = None,
) -> str:
    lang = language or i18n.get_default_language()
    resp = _request(
        {"op": "recognize", "image": image_path, "lang": lang},
        timeout=timeout,
    )
    if "error" in resp:
        raise RuntimeError(resp["error"])
    return resp.get("text", "")


def get_settings(timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    try:
        resp = _request({"op": "settings_get"}, timeout=timeout)
        return resp.get("settings")
    except Exception:
        return None


def set_remote_settings(
    settings: Dict[str, Any], timeout: float = 5.0
) -> Optional[Dict[str, Any]]:
    try:
        resp = _request(
            {"op": "settings_set", "settings": settings}, timeout=timeout
        )
        return resp.get("settings")
    except Exception as exc:
        return {"error": str(exc)}
