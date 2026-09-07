"""Shared fixtures for MaiOCR tests."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(scope="session")
def project_root() -> Path:
    return ROOT


@pytest.fixture
def synthetic_image() -> np.ndarray:
    """Generate a synthetic image with two lines of black text on a
    white background. Used to exercise the OCR pipeline without
    requiring a real model.
    """
    img = np.full((80, 400, 3), 255, dtype=np.uint8)
    # row of "text"
    img[20:30, 20:200] = 0
    img[50:60, 20:300] = 0
    return img
