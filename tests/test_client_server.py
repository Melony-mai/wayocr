"""Tests for the client/server protocol."""
from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from unittest import mock

import cv2
import numpy as np
import pytest

from maiocr import client, paths
from maiocr.server import handle_request


def _box(t, x, y, w, h=16):
    from maiocr.ocr import TextBox
    return TextBox(
        text=t, score=0.9,
        box=[[x, y], [x + w, y], [x + w, y + h], [x, y + h]],
    )


def _real_image(tmp_path: Path) -> Path:
    img = np.full((60, 200, 3), 255, dtype=np.uint8)
    p = tmp_path / "i.png"
    cv2.imwrite(str(p), img)
    return p


def _server_thread(srv: socket.socket, fake_ocr):
    def runner():
        try:
            conn, _ = srv.accept()
            handle_request(conn, fake_ocr)
        except Exception:
            pass
        finally:
            srv.close()
    t = threading.Thread(target=runner, daemon=True)
    t.start()
    return t


def _start_server(tmp_path: Path, fake_ocr) -> tuple[socket.socket, threading.Thread]:
    fake_sock = tmp_path / "test.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(fake_sock))
    srv.listen(1)
    srv.settimeout(5)
    return srv, _server_thread(srv, fake_ocr)


def test_client_request_round_trip(tmp_path, monkeypatch):
    from maiocr.ocr import OcrResult

    fake_ocr = mock.MagicMock()
    fake_ocr.recognize.return_value = OcrResult(boxes=[
        _box("hello", 0, 0, 40),
        _box("world", 50, 0, 40),
    ])
    fake_ocr.loaded = True
    fake_ocr.provider = "CPUExecutionProvider"
    fake_ocr.info.available = False
    fake_ocr.info.device_name = ""

    image_path = _real_image(tmp_path)
    srv, t = _start_server(tmp_path, fake_ocr)
    monkeypatch.setattr(paths, "SOCKET_PATH", tmp_path / "test.sock")

    resp = client.recognize(str(image_path), timeout=5)
    t.join(timeout=5)
    assert "hello" in resp
    assert "world" in resp


def test_client_release_vram_round_trip(tmp_path, monkeypatch):
    fake_ocr = mock.MagicMock()
    fake_ocr.loaded = True
    fake_ocr.provider = "CUDAExecutionProvider"
    fake_ocr.release_vram.return_value = True
    fake_ocr.info.available = True
    fake_ocr.info.device_name = "RTX 4060"

    srv, t = _start_server(tmp_path, fake_ocr)
    monkeypatch.setattr(paths, "SOCKET_PATH", tmp_path / "test.sock")

    resp = client.release_vram(timeout=5)
    t.join(timeout=5)
    assert resp == {"released": True}
    assert fake_ocr.release_vram.called


def test_client_status_round_trip(tmp_path, monkeypatch):
    fake_ocr = mock.MagicMock()
    fake_ocr.loaded = True
    fake_ocr.provider = "CUDAExecutionProvider"
    fake_ocr.info.available = True
    fake_ocr.info.device_name = "RTX 4060"

    srv, t = _start_server(tmp_path, fake_ocr)
    monkeypatch.setattr(paths, "SOCKET_PATH", tmp_path / "test.sock")

    resp = client.status(timeout=5)
    t.join(timeout=5)
    assert resp["engine_loaded"] is True
    assert resp["provider"] == "CUDAExecutionProvider"


def test_server_writes_status_file(tmp_path, monkeypatch):
    from maiocr.ocr import OcrResult

    fake_ocr = mock.MagicMock()
    fake_ocr.recognize.return_value = OcrResult(boxes=[
        _box("hi", 0, 0, 20),
    ])
    fake_ocr.loaded = True
    fake_ocr.provider = "CPUExecutionProvider"
    fake_ocr.info.available = False
    fake_ocr.info.device_name = ""

    image_path = _real_image(tmp_path)
    fake_status = tmp_path / "status.json"
    fake_log = tmp_path / "log.txt"
    fake_pid = tmp_path / "pid"

    monkeypatch.setattr(paths, "STATUS_FILE", fake_status)
    monkeypatch.setattr(paths, "SERVER_LOG", fake_log)
    monkeypatch.setattr(paths, "PID_FILE", fake_pid)

    srv, t = _start_server(tmp_path, fake_ocr)
    monkeypatch.setattr(paths, "SOCKET_PATH", tmp_path / "test.sock")

    client.recognize(str(image_path), timeout=5)
    t.join(timeout=5)
    assert fake_status.exists()
    data = json.loads(fake_status.read_text())
    assert data["engine_loaded"] is True
