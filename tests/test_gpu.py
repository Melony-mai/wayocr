"""Tests for maiocr.gpu GPU detection and is_gpu_active helper."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from maiocr import gpu


def test_detect_returns_dataclass():
    info = gpu.detect()
    assert isinstance(info, gpu.GpuInfo)
    assert info.provider in {
        "CPUExecutionProvider",
        "CUDAExecutionProvider",
        "TensorrtExecutionProvider",
    }
    assert isinstance(info.available, bool)


def test_detect_falls_back_when_no_nvidia_device(monkeypatch):
    monkeypatch.setattr(gpu, "_has_nvidia_device", lambda: False)
    info = gpu.detect()
    assert info.available is False
    assert info.provider == "CPUExecutionProvider"


def test_detect_falls_back_when_no_cuda_provider(monkeypatch):
    monkeypatch.setattr(gpu, "_has_nvidia_device", lambda: True)
    monkeypatch.setattr(gpu, "_query_nvidia_smi", lambda: ("RTX 4060", ""))
    fake_ort = mock.MagicMock()
    fake_ort.get_available_providers.return_value = ["CPUExecutionProvider"]
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    info = gpu.detect()
    assert info.available is False
    assert info.provider == "CPUExecutionProvider"
    assert info.device_name == "RTX 4060"


def test_detect_uses_cuda_when_available(monkeypatch):
    monkeypatch.setattr(gpu, "_has_nvidia_device", lambda: True)
    monkeypatch.setattr(gpu, "_query_nvidia_smi", lambda: ("RTX 4060", ""))
    fake_ort = mock.MagicMock()
    fake_ort.get_available_providers.return_value = [
        "CPUExecutionProvider",
        "CUDAExecutionProvider",
    ]
    fake_ort.get_device.return_value = "GPU"
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    info = gpu.detect()
    assert info.available is True
    assert info.provider == "CUDAExecutionProvider"
    assert info.device_name == "RTX 4060"


def test_is_gpu_active_reads_status_file(monkeypatch, tmp_path):
    fake_status = tmp_path / "status.json"
    fake_status.write_text(json.dumps({
        "engine_loaded": True,
        "provider": "CUDAExecutionProvider",
    }))
    # is_gpu_active does `from maiocr.paths import STATUS_FILE` at call time
    monkeypatch.setattr("maiocr.paths.STATUS_FILE", fake_status)
    monkeypatch.setattr(gpu.shutil, "which", lambda x: None)
    assert gpu.is_gpu_active() is True


def test_is_gpu_active_returns_false_when_no_status(monkeypatch, tmp_path):
    fake_status = tmp_path / "missing.json"
    monkeypatch.setattr("maiocr.paths.STATUS_FILE", fake_status)
    monkeypatch.setattr(gpu.shutil, "which", lambda x: None)
    assert gpu.is_gpu_active() is False
