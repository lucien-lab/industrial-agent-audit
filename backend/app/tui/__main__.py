"""`python -m app.tui` 入口。"""

from __future__ import annotations

import sys

from app.tui.cli import main

if __name__ == "__main__":
    sys.exit(main())
