#!/usr/bin/env python3
"""Entry point for running the CLI straight out of a checkout.

    python src/main.py mod list

Installed users get the `tmc` console script instead (see pyproject.toml); this
exists so the tool works from a clone with nothing installed at all.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tmc_cli.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
