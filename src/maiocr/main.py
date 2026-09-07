"""MaiOCR client entry point: capture -> recognize -> clipboard."""
from __future__ import annotations

import sys
from pathlib import Path

from maiocr import client, i18n, notify, settings
from maiocr.capture import capture


def main() -> int:
    s = settings.get_settings()
    i18n.set_default_language(s.language)

    image = None
    try:
        image = capture()
        text = client.recognize(image)
        if text:
            from maiocr.clipboard import copy

            copy(text)
            if s.notify_on_copy:
                notify.notify(i18n.t("notifications.copied", n=len(text)))
        else:
            if s.notify_on_copy:
                notify.notify(i18n.t("notifications.no_text"))
        return 0
    except Exception as exc:
        msg = i18n.t("notifications.error", error=str(exc))
        notify.notify(msg)
        print(msg, file=sys.stderr)
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
