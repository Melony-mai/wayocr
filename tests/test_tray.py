"""Tests for the tray status file generation logic."""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from maiocr import gpu
from maiocr.paths import STATUS_FILE
from maiocr.tray import _ensure_status_file, _refresh_status


def test_ensure_status_file_creates_when_missing(tmp_path, monkeypatch):
    fake = tmp_path / "status.json"
    monkeypatch.setattr("maiocr.paths.STATUS_FILE", fake)
    _ensure_status_file()
    assert fake.exists()
    data = json.loads(fake.read_text())
    assert data["service"] == "maiocr"
    assert data["engine_loaded"] is False


def test_ensure_status_file_preserves_existing(tmp_path, monkeypatch):
    fake = tmp_path / "status.json"
    fake.write_text(json.dumps({"service": "maiocr", "engine_loaded": True}))
    monkeypatch.setattr("maiocr.paths.STATUS_FILE", fake)
    _ensure_status_file()
    data = json.loads(fake.read_text())
    assert data["engine_loaded"] is True


def test_refresh_status_reads_server(monkeypatch, tmp_path):
    fake = tmp_path / "status.json"
    monkeypatch.setattr("maiocr.paths.STATUS_FILE", fake)
    monkeypatch.setattr("maiocr.client.status", lambda timeout=2.0: {
        "engine_loaded": True,
        "provider": "CUDAExecutionProvider",
    })
    monkeypatch.setattr(gpu, "detect", lambda: mock.MagicMock(
        available=True, provider="CUDAExecutionProvider", device_name="RTX 4060", reason=""
    ))
    out = _refresh_status()
    assert out["engine_loaded"] is True
    assert out["provider"] == "CUDAExecutionProvider"
    assert out["gpu_available"] is True


def test_refresh_status_handles_no_server(monkeypatch, tmp_path):
    fake = tmp_path / "status.json"
    monkeypatch.setattr("maiocr.paths.STATUS_FILE", fake)
    monkeypatch.setattr("maiocr.client.status", lambda timeout=2.0: None)
    monkeypatch.setattr(gpu, "detect", lambda: mock.MagicMock(
        available=False, provider="CPUExecutionProvider", device_name="", reason="no gpu"
    ))
    out = _refresh_status()
    assert out["engine_loaded"] is False
    assert out["provider"] == "unloaded"
    assert out["gpu_available"] is False
