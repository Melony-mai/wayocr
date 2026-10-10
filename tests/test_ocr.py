"""Unit tests for the OCR engine wrapper and data structures."""
from __future__ import annotations

from unittest import mock

import numpy as np

from maiocr.ocr import OCR, OcrResult, TextBox


def test_textbox_geometry():
    box_coords = [[10.0, 20.0], [50.0, 20.0], [50.0, 60.0], [10.0, 60.0]]
    tb = TextBox(text="test", score=0.95, box=box_coords)

    assert tb.x == 10.0
    assert tb.y == 20.0
    assert tb.x2 == 50.0
    assert tb.y2 == 60.0
    assert tb.width == 40.0
    assert tb.height == 40.0
    assert tb.cx == 30.0
    assert tb.cy == 40.0


def test_ocr_result_to_list():
    box_coords = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]
    tb = TextBox(text="hello", score=0.9, box=box_coords)
    res = OcrResult(boxes=[tb])

    items = res.to_list()
    assert len(items) == 1
    assert items[0] == {
        "text": "hello",
        "score": 0.9,
        "box": box_coords,
        "x": 0.0,
        "y": 0.0,
        "w": 10.0,
        "h": 10.0,
    }


def test_ocr_lazy_state():
    ocr = OCR(prefer_gpu=False)
    assert not ocr.loaded
    assert ocr.provider == "unloaded"
    assert ocr.release_vram() is False


def test_ocr_extract_boxes_rapidocr_dataclass():
    class FakeOutput:
        boxes = [
            [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
            [[10.0, 0.0], [20.0, 0.0], [20.0, 10.0], [10.0, 10.0]],
        ]
        txts = ["line1", "line2"]
        scores = [0.95, 0.88]

    extracted = OCR._extract_boxes(FakeOutput())
    assert len(extracted) == 2
    assert extracted[0][1] == "line1"
    assert extracted[0][2] == 0.95
    assert extracted[1][1] == "line2"
    assert extracted[1][2] == 0.88


def test_ocr_extract_boxes_handles_length_mismatch():
    class MismatchedOutput:
        boxes = [
            [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
        ]
        txts = ["line1", "extra_line"]
        scores = [0.95]

    # Should not raise IndexError
    extracted = OCR._extract_boxes(MismatchedOutput())
    assert len(extracted) == 1
    assert extracted[0][1] == "line1"


def test_ocr_extract_boxes_legacy_list_tuple():
    raw_legacy = [
        ([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]], ("hello", 0.92)),
        ([[10.0, 0.0], [20.0, 0.0], [20.0, 10.0], [10.0, 10.0]], ["world", 0.85]),
    ]
    extracted = OCR._extract_boxes(raw_legacy)
    assert len(extracted) == 2
    assert extracted[0][1] == "hello"
    assert extracted[0][2] == 0.92
    assert extracted[1][1] == "world"
    assert extracted[1][2] == 0.85


def test_ocr_extract_boxes_empty_or_invalid():
    assert OCR._extract_boxes(None) == []
    assert OCR._extract_boxes([]) == []
    assert OCR._extract_boxes("invalid") == []


def test_ocr_recognize_and_release(monkeypatch):
    fake_engine = mock.MagicMock()
    fake_engine.return_value = [
        ([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]], ("detected", 0.99))
    ]
    fake_session = mock.MagicMock()
    fake_engine.text_det = fake_session

    ocr = OCR(prefer_gpu=False)
    monkeypatch.setattr(ocr, "_build_engine", lambda: setattr(ocr, "_engine", fake_engine))

    img = np.zeros((50, 50, 3), dtype=np.uint8)
    res = ocr.recognize(img)

    assert ocr.loaded
    assert len(res.boxes) == 1
    assert res.boxes[0].text == "detected"

    assert ocr.release_vram() is True
    assert not ocr.loaded
    assert ocr.provider == "unloaded"
    fake_session.session.close.assert_called_once()
