"""Backward-compatible ``wayocr`` command.

Delegates to ``maiocr.cli`` so that niri / DMS shortcuts that already
reference ``wayocr`` keep working after the rename.
"""
from __future__ import annotations

import sys

from maiocr.cli import main as _cli_main


def main() -> int:
    return _cli_main()


if __name__ == "__main__":
    sys.exit(main())
