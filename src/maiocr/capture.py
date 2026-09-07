"""Capture screenshot via slurp+grim (Wayland)."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from maiocr import i18n


def capture() -> str:
    if not shutil.which("slurp"):
        raise RuntimeError(i18n.t("errors.slurp_missing"))
    if not shutil.which("grim"):
        raise RuntimeError(i18n.t("errors.grim_missing"))

    temp_file = tempfile.NamedTemporaryFile(
        suffix=".png",
        prefix="maiocr_",
        delete=False,
    )
    image_path = Path(temp_file.name)
    temp_file.close()

    try:
        geometry = subprocess.check_output(["slurp"], text=True).strip()
    except subprocess.CalledProcessError:
        raise RuntimeError(i18n.t("errors.capture_cancelled"))
    except FileNotFoundError:
        raise RuntimeError(i18n.t("errors.slurp_missing"))

    if not geometry:
        raise RuntimeError(i18n.t("errors.capture_cancelled"))

    try:
        subprocess.run(
            ["grim", "-g", geometry, str(image_path)],
            check=True,
        )
    except FileNotFoundError:
        raise RuntimeError(i18n.t("errors.grim_missing"))

    return str(image_path)
