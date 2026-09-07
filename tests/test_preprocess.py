"""Tests for preprocess pipeline."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from maiocr.preprocess import preprocess_image


def _save(img: np.ndarray, path: Path) -> None:
    cv2.imwrite(str(path), img)


def test_preprocess_light_on_white(tmp_path: Path):
    img = np.full((100, 1000, 3), 255, dtype=np.uint8)
    cv2.putText(img, "Hello", (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 0), 2)
    p = tmp_path / "white.png"
    _save(img, p)
    out = preprocess_image(str(p))
    assert out is not None
    assert out.shape == (100, 1000)
    assert out.dtype == np.uint8


def test_preprocess_dark_inverts(tmp_path: Path):
    img = np.zeros((60, 200, 3), dtype=np.uint8)
    cv2.putText(img, "Hi", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    p = tmp_path / "dark.png"
    _save(img, p)
    out = preprocess_image(str(p))
    # After invert, the mean should be > 100 (text became dark on light)
    assert float(np.mean(out)) > 100


def test_preprocess_resizes_wide_images(tmp_path: Path):
    big = np.full((100, 4000, 3), 255, dtype=np.uint8)
    p = tmp_path / "wide.png"
    _save(big, p)
    out = preprocess_image(str(p))
    assert out.shape[1] == 1600


def test_preprocess_missing_file_raises(tmp_path: Path):
    with pytest.raises(RuntimeError):
        preprocess_image(str(tmp_path / "nope.png"))
