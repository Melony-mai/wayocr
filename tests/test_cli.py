"""Tests for the top-level CLI dispatch."""
from __future__ import annotations

from unittest import mock

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
    assert rc == 0
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


def test_systemctl_helper(monkeypatch):
    captured = []

    def fake_run(cmd, check=True):
        captured.append((cmd, check))
        return mock.MagicMock(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    cli._systemctl(["restart", "MaiOCR.service"], check=False)
    assert len(captured) == 1
    assert captured[0][0] == ["systemctl", "--user", "restart", "MaiOCR.service"]
    assert captured[0][1] is False


def test_status_format_provider():
    from maiocr.status import _format_provider

    assert "CUDA" in _format_provider("CUDAExecutionProvider")
    assert "TensorRT" in _format_provider("TensorrtExecutionProvider")
    assert "CPU" in _format_provider("CPUExecutionProvider")
    assert "not loaded" in _format_provider("unloaded").lower() or "未加载" in _format_provider("unloaded")
    assert _format_provider("UnknownProvider") == "UnknownProvider"

