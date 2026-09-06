"""tmc — a command-line client for TMC's public content API.

See `cli.py` for the command surface and `docs/` in the website repo
(`docs/api/public-content-api.md`) for the API this speaks to.
"""

from .version import __version__

__all__ = ["__version__", "main"]


def main(argv=None) -> int:
    from .cli import main as _main

    return _main(argv)
