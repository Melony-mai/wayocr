"""GPU detection for MaiOCR.

Auto-detects whether an NVIDIA GPU and the CUDA Execution Provider for
ONNX Runtime are available. The service prefers GPU when both
conditions hold, and silently falls back to CPU otherwise.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class GpuInfo:
    available: bool
    provider: str
    reason: str
    device_name: str = ""
    cuda_version: str = ""


def _has_nvidia_device() -> bool:
    return (
        Path("/dev/nvidia0").exists()
        or Path("/dev/nvidiactl").exists()
    )


def _query_nvidia_smi() -> tuple[str, str]:
    if not shutil.which("nvidia-smi"):
        return "", ""
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version",
                "--format=csv,noheader",
            ],
            text=True,
            timeout=3,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return "", ""
    if not out:
        return "", ""
    first = out.splitlines()[0]
    parts = [p.strip() for p in first.split(",")]
    name = parts[0] if len(parts) >= 1 else ""
    return name, ""


def detect() -> GpuInfo:
    """Return the best available execution provider.

    Order of preference:
        1. CUDAExecutionProvider  (onnxruntime-gpu with CUDA + cuDNN)
        2. TensorrtExecutionProvider
        3. CPUExecutionProvider   (always present)
    """
    if not _has_nvidia_device():
        return GpuInfo(False, "CPUExecutionProvider", "no /dev/nvidia* device")

    name, _ = _query_nvidia_smi()

    try:
        import onnxruntime as ort  # noqa: WPS433
    except Exception as exc:  # pragma: no cover
        return GpuInfo(
            False,
            "CPUExecutionProvider",
            f"onnxruntime import failed: {exc}",
            device_name=name,
        )

    available = ort.get_available_providers()

    for preferred in ("CUDAExecutionProvider", "TensorrtExecutionProvider"):
        if preferred in available:
            cuda_v = ""
            try:
                cuda_v = ort.get_device()  # 'GPU' or 'CPU'
            except Exception:
                cuda_v = ""
            return GpuInfo(
                available=True,
                provider=preferred,
                reason=f"{preferred} reported by onnxruntime",
                device_name=name or cuda_v,
            )

    return GpuInfo(
        False,
        "CPUExecutionProvider",
        f"GPU device present ({name}) but onnxruntime has no CUDA/TRT provider",
        device_name=name,
    )


def is_gpu_active() -> bool:
    """Best-effort check whether a process currently holds VRAM.

    Returns True if our server's last known status had a loaded GPU
    engine, or if nvidia-smi reports non-trivial memory use. Used by
    the tray to display a GPU-active icon.
    """
    try:
        from maiocr.paths import STATUS_FILE
        if STATUS_FILE.exists():
            import json
            data = json.loads(STATUS_FILE.read_text())
            if data.get("engine_loaded") and data.get("provider", "").endswith(
                "ExecutionProvider"
            ) and data.get("provider") != "CPUExecutionProvider":
                return True
    except Exception:
        pass
    if not shutil.which("nvidia-smi"):
        return False
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=2,
        ).strip()
    except Exception:
        return False
    for line in out.splitlines():
        try:
            if int(line.strip()) > 200:  # >200 MB
                return True
        except ValueError:
            continue
    return False


# Re-export of Path import for the helper above.
from pathlib import Path  # noqa: E402
