"""MaiOCR client entry point: capture -> recognize -> clipboard."""
from __future__ import annotations

import sys
from pathlib import Path

from maiocr import client, i18n, notifications, settings
from maiocr.capture import capture


def main() -> int:
    s = settings.get_settings()
    i18n.set_default_language(s.language)
    notifications.initialise()

    image = None
    try:
        image = capture()
        text = client.recognize(image)
        if text:
            from maiocr.clipboard import copy

            copy(text)
            if s.notify_on_copy:
                # In system mode this fires a notify-send pop-up
                # directly.  In custom mode the CLI process has no
                # Qt event loop, so we drop a request file for the
                # tray process to pick up and show the bubble.
                if s.notification_mode == "custom":
                    notifications.request_ocr_completed(len(text))
                else:
                    notifications.notify_ocr_completed(len(text))
        else:
            if s.notify_on_copy:
                notifications.notify_ocr_empty()
        return 0
    except Exception as exc:
        notifications.notify_error(f"{exc}")
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if image:
            try:
                Path(image).unlink(missing_ok=True)
            except Exception:
                pass
            stem = Path(image).stem
            for sibling in Path(image).parent.glob(f"{stem}_processed.png"):
                try:
                    sibling.unlink()
                except Exception:
                    pass


if __name__ == "__main__":
    sys.exit(main())
