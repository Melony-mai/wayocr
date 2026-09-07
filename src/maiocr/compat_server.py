"""Backward-compatible ``wayocr-server`` entry point."""
from __future__ import annotations

import sys

from maiocr.server import main as _server_main


def main() -> int:
    return _server_main()


if __name__ == "__main__":
    sys.exit(main())
