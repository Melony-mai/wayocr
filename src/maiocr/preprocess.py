"""Image preprocessing for OCR.

The previous implementation always converted to grayscale and
contrast-stretched, which is destructive for:

* light text on dark backgrounds (terminal screenshots)
* low-contrast colored text
* code with syntax highlighting

This module inspects the image and picks a pipeline:

* if the image is already high contrast (e.g. terminal black/white) we
  send it as-is,
* if the image is colored but low contrast, we use the luma channel
  (Y from YCrCb) which preserves perceptual brightness while removing
  color noise,
* if it's very dark or very bright we apply a CLAHE local-contrast
  stretch on the luma channel.
"""
from __future__ import annotations

from pathlib import Path
from typing import Tuple

import cv2
import numpy as np


def _read(path: str) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"无法读取图片: {path}")
    return img


def _resize(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    max_width = 1600

    if w > max_width:
        scale = max_width / w
        return cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    if w < 800:
        scale = 1.5
        return cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    return img


def _luma(img_bgr: np.ndarray) -> np.ndarray:
    ycrcb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2YCrCb)
    return ycrcb[:, :, 0]


def _clahe(gray: np.ndarray) -> np.ndarray:
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def _luma_range(gray: np.ndarray) -> Tuple[float, float]:
    return float(np.min(gray)), float(np.max(gray))


def _invert_if_dark(gray: np.ndarray) -> np.ndarray:
    """If the image is predominantly dark (terminal), invert it so that
    OCR models that expect black-on-white text behave well."""
    mean = float(np.mean(gray))
    if mean < 90:
        return cv2.bitwise_not(gray)
    return gray


def preprocess_image(image_path: str) -> np.ndarray:
    """Return a grayscale, contrast-improved numpy array ready for OCR."""
    img = _read(image_path)
    img = _resize(img)
    gray = _luma(img)
    lo, hi = _luma_range(gray)

    if hi - lo < 60:
        gray = _clahe(gray)
        lo, hi = _luma_range(gray)

    gray = _invert_if_dark(gray)

    if hi - lo < 60:
        gray = _clahe(gray)

    return gray


def preprocess_to_file(image_path: str) -> str:
    """Legacy path-based entry: write the preprocessed image next to the
    original and return its path. Used by the server for debugging.
    """
    arr = preprocess_image(image_path)
    path = Path(image_path)
    out = path.parent / f"{path.stem}_processed.png"
    cv2.imwrite(str(out), arr)
    return str(out)
