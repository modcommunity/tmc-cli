"""What every command is handed: parsed flags, plus a client built on demand.

The client is lazy because half the commands do not need one. `auth login`,
`schema` and `completion` all run before there is a credential to build, and
resolving one eagerly would make "I have not logged in yet" an error on the very
command that fixes it.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Sequence

from .client import ContentClient
from .config import Settings, resolve
from .http import Transport
from . import output


class Context:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self._settings: Settings | None = None
        self._client: ContentClient | None = None

    # -- lazy plumbing -------------------------------------------------------

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = resolve(self.args)

        return self._settings

    @property
    def client(self) -> ContentClient:
        if self._client is None:
            settings = self.settings

            transport = Transport(
                settings.base_url,
                settings.credential,
                timeout=settings.timeout,
                retries=settings.retries,
                retry_wait_max=settings.retry_wait_max,
                verify_tls=settings.verify_tls,
                debug=settings.debug,
                dry_run=settings.dry_run,
            )

            self._client = ContentClient(transport, progress=self.progress)

        return self._client

    # -- output --------------------------------------------------------------

    @property
    def quiet(self) -> bool:
        return bool(getattr(self.args, "quiet", False))

    @property
    def dry_run(self) -> bool:
        return bool(getattr(self.args, "dry_run", False))

    def progress(self, message: str) -> None:
        if not self.quiet:
            output.info(f"… {message}")

    def note(self, message: str) -> None:
        if not self.quiet:
            output.info(message)

    def done(self, message: str) -> None:
        # A dry run has already printed what it WOULD send; announcing that a
        # thing was created when nothing was is worse than saying nothing.
        if not self.quiet and not self.dry_run:
            output.success(message)

    def emit(
        self,
        payload: Any,
        *,
        columns: Sequence[str] | None = None,
        pagination: dict[str, Any] | None = None,
    ) -> None:
        """Render a result, and summarise pagination on stderr when there is any."""

        # Nothing came back from a dry run, and "(no results)" would read as an
        # empty answer rather than as a request that was never sent.
        if self.dry_run and payload is None:
            return

        fmt = getattr(self.args, "output", "table")
        fields = getattr(self.args, "field", None)

        output.render(payload, fmt=fmt, columns=columns, fields=fields)

        if pagination and not self.quiet and fmt == "table":
            total = pagination.get("total")
            page = pagination.get("page")
            pages = pagination.get("totalPages")
            shown = len(payload) if isinstance(payload, list) else 1

            output.info(f"{shown} shown · {total} total · page {page}/{pages}")

    def confirm(self, prompt: str) -> bool:
        """Ask before something irreversible, unless --yes or not a terminal.

        Non-interactive runs (CI, a cron job) get the safe answer rather than a
        hang: no `--yes` and no tty means no.
        """

        if getattr(self.args, "yes", False):
            return True

        if not sys.stdin.isatty():
            output.error(
                "Refusing to continue without confirmation "
                "(not a terminal). Pass --yes to proceed."
            )
            return False

        answer = input(f"{prompt} [y/N] ").strip().lower()

        return answer in ("y", "yes")
