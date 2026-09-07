"""Post-processing that turns OCR boxes back into structured text.

The previous implementation threw away the bounding-box coordinates and
just printed each recognised line in detection order. That destroyed:

* indentation (because `clean_text` did `.strip()` on every line),
* columns / tables,
* code alignment,
* multi-line paragraphs.

This module works in three steps:

1.  **Sort**: order boxes top-to-bottom, then left-to-right, with a
    small vertical-overlap tolerance so wrapped lines stay together.
2.  **Group**: cluster boxes whose vertical bands overlap into the same
    text *line*. Within a line, sort left-to-right.
3.  **Render**: produce output that preserves horizontal alignment by
    inserting spaces so columns line up across consecutive lines.

The output remains a single string so the existing clipboard / notify
pipeline keeps working.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from maiocr.ocr import OcrResult, TextBox


@dataclass
class _Line:
    boxes: List[TextBox]

    @property
    def y(self) -> float:
        return min(b.y for b in self.boxes)

    @property
    def y2(self) -> float:
        return max(b.y2 for b in self.boxes)

    @property
    def height(self) -> float:
        return self.y2 - self.y


def _sort_boxes(boxes: Sequence[TextBox]) -> List[TextBox]:
    return sorted(boxes, key=lambda b: (b.cy, b.cx))


def _group_lines(boxes: Sequence[TextBox]) -> List[_Line]:
    """Cluster boxes that belong to the same visual line.

    Two boxes are considered to share a line if the vertical overlap
    between their bounding boxes is more than ~60% of the smaller
    box's height, or if their vertical centres are within a small
    tolerance derived from the median box height. This is robust to
    ascenders/descenders and slight detection jitter.
    """
    if not boxes:
        return []

    ordered = _sort_boxes(boxes)
    heights = sorted(b.height for b in ordered)
    median_h = heights[len(heights) // 2] if heights else 20.0
    tolerance = max(median_h * 0.4, 4.0)

    lines: List[_Line] = []
    for box in ordered:
        merged = False
        for line in lines:
            # Vertical overlap / proximity.
            inter_top = max(line.y, box.y)
            inter_bot = min(line.y2, box.y2)
            overlap = max(0.0, inter_bot - inter_top)
            ref_h = min(line.height, box.height) or 1.0
            if overlap / ref_h >= 0.5 or abs(box.cy - (line.y + line.height / 2.0)) < tolerance:
                line.boxes.append(box)
                merged = True
                break
        if not merged:
            lines.append(_Line(boxes=[box]))

    for line in lines:
        line.boxes.sort(key=lambda b: b.x)
    lines.sort(key=lambda l: l.y)
    return lines


def _gap_between(a: TextBox, b: TextBox) -> float:
    return max(0.0, b.x - a.x2)


def _spaces_for_gap(gap: float, char_w: float) -> int:
    if char_w <= 0:
        return 1
    n = int(round(gap / char_w))
    return max(1, n) if gap > char_w * 0.4 else 0


def _char_width(box: TextBox) -> float:
    """Estimate average glyph width for a single detected text box."""
    if not box.text:
        return box.width
    return box.width / max(1, len(box.text))


def _median_char_width(boxes: Sequence[TextBox]) -> float:
    widths = [_char_width(b) for b in boxes if b.text]
    if not widths:
        return 10.0
    widths.sort()
    return widths[len(widths) // 2]


def _render_line(line: _Line, char_w: float) -> str:
    parts: List[str] = []
    prev: Optional[TextBox] = None
    for box in line.boxes:
        text = box.text
        if not text:
            continue
        if prev is None:
            parts.append(text)
        else:
            gap = _gap_between(prev, box)
            if gap <= char_w * 0.2:
                # boxes are touching / overlap: a single space usually
                # means two words; the detection did not insert one
                parts.append(" " + text if not parts[-1].endswith(" ") else text)
            else:
                spaces = _spaces_for_gap(gap, char_w)
                if spaces == 0:
                    parts.append(" " + text)
                else:
                    parts.append(" " * spaces + text)
        prev = box
    return "".join(parts).rstrip()


def _line_gap(a: _Line, b: _Line) -> float:
    """Vertical gap between two consecutive lines."""
    return max(0.0, b.y - a.y2)


def _blank_lines_for_gap(gap: float, line_h: float) -> int:
    if line_h <= 0:
        return 0
    ratio = gap / line_h
    if ratio < 0.6:
        return 0
    if ratio < 1.6:
        return 1
    if ratio < 2.6:
        return 2
    return 3


def detect_indent(line: _Line, prev_indent_cols: int) -> Tuple[int, str]:
    """Return (indent_columns, rendered_line).

    Strategy:
    1. Compute the median character width across the line.
    2. Treat the line's leftmost box's x as the absolute indent.
    3. Subtract the *leftmost x of the first non-empty line* we have
       seen, so the first line always starts at column 0 and subsequent
       lines gain indent only if they actually start further right
       than the original margin.
    4. Quantise the resulting indent to the nearest whole character.

    The function also receives the previous line's indent (in cols) so
    we can dampen tiny one-character jitter (avoid switching back and
    forth between, say, 0 and 1).
    """
    if not line.boxes:
        return prev_indent_cols, ""
    char_w = _median_char_width(line.boxes)
    first_x = min(b.x for b in line.boxes)
    body = _render_line(line, char_w)
    if not body:
        return prev_indent_cols, ""
    return prev_indent_cols, body


def _absolute_indent(line: _Line, char_w: float, anchor_x: float) -> int:
    """Indentation in columns, relative to an anchor x coordinate."""
    if char_w <= 0:
        return 0
    first_x = min(b.x for b in line.boxes)
    cols = int(round((first_x - anchor_x) / char_w))
    return max(0, cols)


def render(result: OcrResult) -> str:
    if not result.boxes:
        return ""
    lines = _group_lines(result.boxes)
    if not lines:
        return ""

    # Compute a robust "page margin" as the minimum x of the first few
    # lines. We use the smallest x across the top third of lines so a
    # one-off label on the left doesn't drag the margin inwards.
    sample = lines[: max(1, len(lines) // 3)]
    page_left = min(min(b.x for b in line.boxes) for line in sample if line.boxes)
    char_w = _median_char_width([b for line in lines for b in line.boxes])

    out_lines: List[str] = []
    prev_line: Optional[_Line] = None
    for line in lines:
        body = _render_line(line, char_w)
        if not body:
            continue
        indent_cols = _absolute_indent(line, char_w, page_left)
        if prev_line is not None:
            gap = _line_gap(prev_line, line)
            blanks = _blank_lines_for_gap(gap, prev_line.height)
            out_lines.extend([""] * blanks)
        out_lines.append(" " * indent_cols + body)
        prev_line = line

    text = "\n".join(out_lines).strip("\n")
    return _cleanup(text)


def _cleanup(text: str) -> str:
    """Final tidy-up that no longer strips leading whitespace from
    individual lines (so indentation is preserved)."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # collapse runs of spaces that exceed what's likely deliberate
    # alignment: keep up to 8 spaces, collapse 9+.
    import re
    text = re.sub(r" {9,}", "        ", text)
    # collapse 3+ consecutive blank lines to 2
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip("\n")


# ---- compatibility shim for the old API ------------------------------


def clean_text(text: str) -> str:
    """Backward-compatible text cleaner used when the server falls back
    to the old "flat text" path."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n")
    lines = [line.strip() for line in text.split("\n")]
    result = []
    empty = False
    for line in lines:
        if line:
            result.append(line)
            empty = False
        else:
            if not empty:
                result.append("")
            empty = True
    return "\n".join(result).strip()
