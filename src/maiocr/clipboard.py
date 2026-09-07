"""Clipboard helper (Linux wl-copy, Windows pyperclip)."""
from __future__ import annotations

import platform
import shutil
import subprocess

from maiocr import i18n


def copy(text: str) -> None:
    if not text:
        return
    system = platform.system()

    if system == "Windows":
        import pyperclip  # type: ignore

        pyperclip.copy(text)
        return

    if shutil.which("wl-copy"):
        subprocess.run(["wl-copy"], input=text.encode("utf-8"), check=True)
        return

    if shutil.which("xclip"):
        subprocess.run(
            ["xclip", "-selection", "clipboard"],
            input=text.encode("utf-8"),
            check=True,
        )
        return

    raise RuntimeError(i18n.t("errors.no_clipboard_tool"))
