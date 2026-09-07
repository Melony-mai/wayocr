"""Tests for the tray callback wiring.

We do not import PyQt6 here (it requires a display). Instead we patch
the heavy bits out and assert that the dispatch table points at the
real functions, not at no-ops.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest


def test_tray_module_exposes_callback_helpers():
    """The tray must define real, callable functions for every menu item."""
    from maiocr import tray

    # Each callback used by the right-click menu must exist and be callable.
    # ``open_preferences`` is intentionally inside the PyQt6 closure and is
    # covered by the broader test suite; the actions that fire sub-commands
    # are exposed at module level.
    for name in (
        "release_vram",
        "restart_service",
        "start_service",
        "stop_service",
        "show_status",
        "show_gpu_info",
        "run_ocr",
        "format_status_text",
        "format_gpu_text",
    ):
        assert hasattr(tray, name), f"missing tray callback: {name}"
        assert callable(getattr(tray, name)), f"{name} is not callable"


def test_tray_release_vram_handles_no_response(monkeypatch):
    from maiocr import tray
    from maiocr import notify

    monkeypatch.setattr(
        "maiocr.client.release_vram",
        lambda timeout=10.0: None,
    )
    captured = []
    monkeypatch.setattr(
        notify,
        "notify",
        lambda msg, title=None: captured.append(msg),
    )
    tray.release_vram()
    assert any("not running" in m or "not loaded" in m or "error" in m for m in captured)


def test_tray_release_vram_reports_success(monkeypatch):
    from maiocr import tray
    from maiocr import notify

    monkeypatch.setattr(
        "maiocr.client.release_vram",
        lambda timeout=10.0: {"released": True},
    )
    captured = []
    monkeypatch.setattr(
        notify,
        "notify",
        lambda msg, title=None: captured.append(msg),
    )
    tray.release_vram()
    assert any("released" in m.lower() or "释放" in m for m in captured)


def test_clean_logs_command(tmp_path, monkeypatch):
    """``maiocr clean-logs`` should call the rotation helper and exit 0."""
    fake_log = tmp_path / "server.log"
    fake_log.write_text("hello\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from maiocr import paths, settings as settings_mod

    monkeypatch.setattr(paths, "SERVER_LOG", fake_log)
    settings_mod._CURRENT = None

    proc = subprocess.run(
        [sys.executable, "-m", "maiocr.cli", "clean-logs"],
        capture_output=True, text=True,
        env={**os.environ, "XDG_CONFIG_HOME": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    assert "logs rotated" in proc.stdout


def test_format_status_text_mentions_engine(monkeypatch):
    from maiocr import tray
    from maiocr import system_info
    monkeypatch.setattr(
        system_info,
        "service_status",
        lambda: {
            "service": {"systemctl": "active", "enabled": "enabled", "since": "today"},
            "runtime": {"exists": True, "gpu_available": True, "gpu_device": "RTX 4060"},
            "pid": 1234,
            "socket": "ok",
            "socket_path": "/tmp/x",
            "ping": "ok",
            "server": {"engine_loaded": True, "provider": "CUDAExecutionProvider"},
        },
    )
    text = tray.format_status_text()
    assert "MaiOCR" in text
    assert "1234" in text
    assert "CUDAExecutionProvider" in text
    assert "RTX 4060" in text


def test_format_gpu_text_reports_unavailable(monkeypatch):
    from maiocr import tray
    from maiocr import system_info
    monkeypatch.setattr(
        system_info,
        "gpu_runtime",
        lambda: {
            "available": False,
            "provider": "CPUExecutionProvider",
            "provider_reason": "no gpu",
            "device_name": "",
            "onnx_providers": ["CPUExecutionProvider"],
            "vram_used_mb": None,
            "vram_total_mb": None,
            "gpu_util_pct": None,
            "temperature_c": None,
            "power_w": None,
            "engine_using_gpu": False,
            "engine_provider": "unloaded",
        },
    )
    text = tray.format_gpu_text()
    assert "GPU" in text or "gpu" in text.lower()
    assert "CPUExecutionProvider" in text
    assert "unavailable" in text.lower() or "不可用" in text


def test_run_ocr_spawns_process(monkeypatch):
    from maiocr import tray

    captured = []
    class _FakePopen:
        def __init__(self, args, *a, **kw):
            captured.append(args)

    monkeypatch.setattr(tray.subprocess, "Popen", _FakePopen)
    tray.run_ocr()
    assert captured and "maiocr.cli" in captured[0]
