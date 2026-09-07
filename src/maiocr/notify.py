"""Desktop notification helper (localized)."""
from __future__ import annotations

import platform
import shutil
import subprocess

from maiocr import i18n


def notify(message: str, title: str | None = None) -> None:
    """Show a desktop notification.

    ``title`` defaults to the localised "MaiOCR" app name.
    """
    if title is None:
        title = i18n.t("app.name")
    system = platform.system()

    if system == "Windows":
        try:
            from win10toast import ToastNotifier  # type: ignore

            ToastNotifier().show_toast(title, message, duration=3)
        except Exception:
            print(f"[{title}] {message}")
        return

    if shutil.which("notify-send"):
        subprocess.run(["notify-send", title, message])
        return

    print(f"[{title}] {message}")


if __name__ == "__main__":  # pragma: no cover
    import sys
    notify(sys.argv[1] if len(sys.argv) > 1 else i18n.t("app.about"))
