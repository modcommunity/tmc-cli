"""Allows `python -m tmc_cli …`."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
