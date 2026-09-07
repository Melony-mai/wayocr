"""Tests for the top-level CLI dispatch."""
from __future__ import annotations

from unittest import mock

import pytest

from maiocr import cli


def test_dispatch_release_vram_calls_client(monkeypatch):
    monkeypatch.setattr("maiocr.client.release_vram", lambda timeout=10.0: {"released": True})
    rc = cli.main(["release-vram"])
    assert rc == 0


def test_dispatch_ping(monkeypatch):
    monkeypatch.setattr("maiocr.client.is_running", lambda: True)
    rc = cli.main(["ping"])
    assert rc == 0

    monkeypatch.setattr("maiocr.client.is_running", lambda: False)
    rc = cli.main(["ping"])
    assert rc == 1


def test_dispatch_gpu_info(monkeypatch, capsys):
    monkeypatch.setattr("maiocr.gpu.detect", lambda: mock.MagicMock(
        available=True, provider="CUDAExecutionProvider", device_name="RTX 4060", reason="ok"
    ))
    rc = cli.main(["gpu-info"])
    out = capsys.readouterr().out
    assert "CUDAExecutionProvider" in out
    assert "RTX 4060" in out


def test_dispatch_default_is_run(monkeypatch):
    called = {"v": False}

    def fake_run_main():
        called["v"] = True
        return 0

    monkeypatch.setattr("maiocr.main.main", fake_run_main)
    rc = cli.main([])
    assert rc == 0
    assert called["v"] is True
