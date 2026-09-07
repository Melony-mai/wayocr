"""Detailed runtime information for the tray and the status command.

The data here is what the **Service Status** and **GPU Information**
tray menu items render. Everything is collected lazily so a missing
``nvidia-smi`` or a stopped service does not blow up the dialog.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from maiocr import client, gpu
from maiocr.paths import PID_FILE, SOCKET_PATH, STATUS_FILE


# --- nvidia-smi helpers --------------------------------------------------


def _have_nvidia_smi() -> bool:
    return shutil.which("nvidia-smi") is not None


def nvidia_smi_query(fields: List[str]) -> Optional[Dict[str, str]]:
    """Run ``nvidia-smi --query-gpu=<fields>`` and return a dict.

    Returns ``None`` if nvidia-smi is missing or the call fails.
    """
    if not _have_nvidia_smi():
        return None
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                f"--query-gpu={','.join(fields)}",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=3,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return None
    rows = out.splitlines()
    if not rows:
        return None
    values = [v.strip() for v in rows[0].split(",")]
    if len(values) != len(fields):
        return None
    return dict(zip(fields, values))


def gpu_runtime() -> Dict[str, Any]:
    """Live GPU runtime information for the **GPU Information** dialog."""
    info = gpu.detect()
    runtime: Dict[str, Any] = {
        "available": info.available,
        "provider": info.provider,
        "provider_reason": info.reason,
        "device_name": info.device_name,
        "onnx_providers": [],
        "vram_used_mb": None,
        "vram_total_mb": None,
        "gpu_util_pct": None,
        "temperature_c": None,
        "power_w": None,
        "engine_using_gpu": False,
        "engine_provider": None,
    }
    try:
        import onnxruntime as ort  # type: ignore
        runtime["onnx_providers"] = list(ort.get_available_providers())
    except Exception:
        pass

    if info.available and _have_nvidia_smi():
        data = nvidia_smi_query(
            [
                "memory.used",
                "memory.total",
                "utilization.gpu",
                "temperature.gpu",
                "power.draw",
            ]
        )
        if data:
            try:
                runtime["vram_used_mb"] = int(data["memory.used"])
                runtime["vram_total_mb"] = int(data["memory.total"])
            except (KeyError, ValueError):
                pass
            try:
                runtime["gpu_util_pct"] = int(data["utilization.gpu"])
            except (KeyError, ValueError):
                pass
            try:
                runtime["temperature_c"] = int(data["temperature.gpu"])
            except (KeyError, ValueError):
                pass
            try:
                runtime["power_w"] = float(data["power.draw"])
            except (KeyError, ValueError):
                pass

    # engine state from the running server
    try:
        srv = client.status(timeout=1.0) or {}
    except Exception:
        srv = {}
    runtime["engine_provider"] = srv.get("provider", "unloaded")
    runtime["engine_using_gpu"] = bool(
        srv.get("engine_loaded")
        and srv.get("provider") in ("CUDAExecutionProvider", "TensorrtExecutionProvider")
    )
    return runtime


# --- service status helpers ---------------------------------------------


def _systemctl_state() -> Dict[str, Any]:
    """Resolve the systemd user service state, falling back to socket probing."""
    state: Dict[str, Any] = {
        "service": "MaiOCR.service",
        "active": False,
        "systemctl": None,
        "enabled": None,
        "since": None,
        "main_pid": None,
    }
    if not shutil.which("systemctl"):
        state["systemctl"] = "not found"
    else:
        for name in ("MaiOCR.service", "maiocr.service"):
            try:
                proc = subprocess.run(
                    ["systemctl", "--user", "is-active", name],
                    text=True,
                    capture_output=True,
                )
            except Exception as exc:
                state["systemctl"] = f"error: {exc}"
                break
            if proc.returncode in (0, 3):
                state["systemctl"] = (proc.stdout or proc.stderr or "").strip()
                state["active"] = proc.returncode == 0
                break
        # is-enabled (best effort)
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "is-enabled", "MaiOCR.service"],
                text=True,
                capture_output=True,
            )
            state["enabled"] = (proc.stdout or "").strip() or None
        except Exception:
            pass
        # show --property to get main PID and since
        try:
            proc = subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    "MaiOCR.service",
                    "-p",
                    "MainPID,ActiveEnterTimestamp",
                    "--value",
                ],
                text=True,
                capture_output=True,
            )
            out = (proc.stdout or "").strip()
            lines = out.splitlines()
            if lines and lines[0].isdigit():
                state["main_pid"] = int(lines[0])
            if len(lines) > 1 and lines[1]:
                state["since"] = lines[1]
        except Exception:
            pass

    # socket liveness
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect(str(SOCKET_PATH))
        state["socket"] = "ok"
        state["socket_path"] = str(SOCKET_PATH)
    except Exception as exc:
        state["socket"] = f"unavailable ({exc.__class__.__name__})"
        state["socket_path"] = str(SOCKET_PATH)
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return state


def _read_status_file() -> Dict[str, Any]:
    if not STATUS_FILE.exists():
        return {"exists": False}
    try:
        data = json.loads(STATUS_FILE.read_text())
        data["exists"] = True
        data["mtime"] = STATUS_FILE.stat().st_mtime
        return data
    except Exception as exc:
        return {"exists": True, "error": str(exc)}


def service_status() -> Dict[str, Any]:
    """Aggregate everything the **Service Status** dialog needs."""
    result: Dict[str, Any] = {
        "collected_at": time.time(),
        "service": _systemctl_state(),
        "runtime": _read_status_file(),
        "socket_path": str(SOCKET_PATH),
        "pid_file": str(PID_FILE),
        "pid_file_exists": PID_FILE.exists(),
    }
    if PID_FILE.exists():
        try:
            result["pid"] = int(PID_FILE.read_text().strip())
        except Exception:
            result["pid"] = None

    # Ping the server for a live reply
    try:
        if client.is_running(timeout=0.5):
            result["ping"] = "ok"
            info = client.status(timeout=1.0) or {}
            result["server"] = info
        else:
            result["ping"] = "no response"
    except Exception as exc:
        result["ping"] = f"error: {exc}"
    return result
