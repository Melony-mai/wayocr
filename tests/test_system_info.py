"""Tests for the system_info helpers used by the tray dialogs."""
from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from maiocr import paths, system_info
from maiocr.paths import SOCKET_PATH, STATUS_FILE


def test_nvidia_smi_query_returns_none_when_missing(monkeypatch):
    monkeypatch.setattr(system_info.shutil, "which", lambda _: None)
    assert system_info.nvidia_smi_query(["memory.used"]) is None


def test_nvidia_smi_query_parses_first_row(monkeypatch):
    fake = "/usr/bin/nvidia-smi"
    monkeypatch.setattr(system_info.shutil, "which", lambda _: fake)
    fake_out = "256, 8192, 0, 41, 7.20\n"
    monkeypatch.setattr(
        subprocess,
        "check_output",
        mock.Mock(return_value=fake_out),
    )
    out = system_info.nvidia_smi_query(
        ["memory.used", "memory.total", "utilization.gpu", "temperature.gpu", "power.draw"]
    )
    assert out == {
        "memory.used": "256",
        "memory.total": "8192",
        "utilization.gpu": "0",
        "temperature.gpu": "41",
        "power.draw": "7.20",
    }


def test_gpu_runtime_includes_vram_when_available(monkeypatch):
    monkeypatch.setattr(system_info.shutil, "which", lambda _: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(
        system_info,
        "nvidia_smi_query",
        lambda fields: {
            "memory.used": "1024",
            "memory.total": "8192",
            "utilization.gpu": "12",
            "temperature.gpu": "55",
            "power.draw": "10.5",
        },
    )
    monkeypatch.setattr(
        system_info.gpu,
        "detect",
        lambda: mock.MagicMock(
            available=True,
            provider="CUDAExecutionProvider",
            device_name="RTX 4060",
            reason="cuda ok",
        ),
    )
    monkeypatch.setattr(
        system_info.client,
        "status",
        lambda timeout=1.0: {"engine_loaded": True, "provider": "CUDAExecutionProvider"},
    )
    out = system_info.gpu_runtime()
    assert out["vram_used_mb"] == 1024
    assert out["vram_total_mb"] == 8192
    assert out["engine_using_gpu"] is True
    assert out["provider"] == "CUDAExecutionProvider"


def test_gpu_runtime_reports_unavailable_cleanly(monkeypatch):
    monkeypatch.setattr(system_info.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        system_info.gpu,
        "detect",
        lambda: mock.MagicMock(
            available=False,
            provider="CPUExecutionProvider",
            device_name="",
            reason="no gpu",
        ),
    )
    monkeypatch.setattr(
        system_info.client,
        "status",
        lambda timeout=1.0: {"engine_loaded": False, "provider": "unloaded"},
    )
    out = system_info.gpu_runtime()
    assert out["available"] is False
    assert out["vram_used_mb"] is None
    assert out["engine_using_gpu"] is False
    assert "CPUExecutionProvider" in out["onnx_providers"] or out["onnx_providers"] == []


def test_service_status_collects_sections(monkeypatch, tmp_path):
    # Use a tmp SOCKET/STATUS to avoid touching the live system
    fake_sock = tmp_path / "sock"
    fake_status = tmp_path / "status.json"
    fake_status.write_text(
        json.dumps(
            {
                "engine_loaded": False,
                "provider": "unloaded",
                "gpu_available": True,
                "gpu_device": "RTX 4060",
            }
        )
    )
    monkeypatch.setattr(system_info, "SOCKET_PATH", fake_sock)
    monkeypatch.setattr(system_info, "STATUS_FILE", fake_status)
    monkeypatch.setattr(system_info, "PID_FILE", tmp_path / "pid")

    monkeypatch.setattr(system_info.shutil, "which", lambda _: None)
    monkeypatch.setattr(system_info.client, "is_running", lambda timeout=0.5: False)
    data = system_info.service_status()
    assert "service" in data
    assert "runtime" in data
    assert data["runtime"].get("gpu_available") is True
    assert data["ping"] != "ok"  # we forced it to fail
