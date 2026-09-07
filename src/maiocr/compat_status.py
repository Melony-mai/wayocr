"""Backward-compatible ``wayocr-status`` entry point."""
from __future__ import annotations

import sys

from maiocr.status import main as _status_main


def main() -> int:
    return _status_main()


if __name__ == "__main__":
    sys.exit(main())
