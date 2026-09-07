"""Tests for the OCR formatting / processor pipeline."""
from __future__ import annotations

import pytest

from maiocr.ocr import OcrResult, TextBox
from maiocr.processor import _group_lines, render


def _box(text: str, x: float, y: float, w: float, h: float = 16.0) -> TextBox:
    return TextBox(
        text=text,
        score=0.9,
        box=[[x, y], [x + w, y], [x + w, y + h], [x, y + h]],
    )


def test_render_empty():
    assert render(OcrResult(boxes=[])) == ""


def test_render_preserves_indentation():
    boxes = [
        _box("def f():", x=0, y=0, w=60),
        _box("return 1", x=20, y=20, w=70),
    ]
    out = render(OcrResult(boxes=boxes))
    assert "def f():" in out
    # Indent should be at least one space before "return 1"
    indented = [line for line in out.splitlines() if "return" in line]
    assert indented and indented[0].startswith(" " * 1)


def test_render_anchor_uses_page_left():
    # The first line's x is 100 (e.g. due to a margin), the second at
    # 100 too. Both should be at column 0 in the output.
    boxes = [
        _box("alpha", x=100, y=0, w=50),
        _box("beta", x=100, y=20, w=50),
    ]
    out = render(OcrResult(boxes=boxes))
    for line in out.splitlines():
        assert not line.startswith(" "), line


def test_render_indent_for_inset_line():
    # First line at x=0 (the page anchor), second line at x=40 (a
    # clearly indented body line).
    boxes = [
        _box("if True:", x=0, y=0, w=60),
        _box("print(1)", x=40, y=20, w=60),
    ]
    out = render(OcrResult(boxes=boxes))
    lines = out.splitlines()
    assert lines[0] == "if True:"
    assert lines[1].startswith(" "), lines[1]
    assert lines[1].lstrip().startswith("print")


def test_render_orders_top_to_bottom():
    boxes = [
        _box("second", x=0, y=40, w=60),
        _box("first", x=0, y=0, w=60),
    ]
    out = render(OcrResult(boxes=boxes))
    lines = out.splitlines()
    assert lines.index("first") < lines.index("second")


def test_render_orders_left_to_right_within_line():
    boxes = [
        _box("B", x=80, y=0, w=20),
        _box("A", x=0, y=0, w=20),
    ]
    out = render(OcrResult(boxes=boxes))
    assert out.splitlines()[0].strip().startswith("A")


def test_render_inserts_blank_between_paragraphs():
    boxes = [
        _box("para1", x=0, y=0, w=50, h=20),
        _box("para2", x=0, y=200, w=50, h=20),
    ]
    out = render(OcrResult(boxes=boxes))
    # Should have at least one blank line between them
    parts = out.split("para1", 1)[1]
    assert "\n\n" in parts


def test_group_lines_merges_overlapping_boxes():
    boxes = [
        _box("hello", x=0, y=0, w=40),
        _box("world", x=50, y=1, w=40),
    ]
    lines = _group_lines(boxes)
    assert len(lines) == 1
    assert [b.text for b in lines[0].boxes] == ["hello", "world"]


def test_group_lines_splits_vertical_gap():
    boxes = [
        _box("a", x=0, y=0, w=20, h=20),
        _box("b", x=0, y=100, w=20, h=20),
    ]
    lines = _group_lines(boxes)
    assert len(lines) == 2


def test_clean_text_legacy_path():
    from maiocr.processor import clean_text

    out = clean_text("  hello  \n\n  world  ")
    assert out == "hello\n\nworld"
