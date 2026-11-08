"""`tmc integration …` — the operator-facing half of `/api/integration/v1`.

The integration API (website-city `docs/api/integration-api.md`) takes a
per-server or per-app integration key (`tmci_…`), not the content key or an
app-user session. The key is read per call from `--key-file` or
`TMC_INTEGRATION_KEY` and never written to the profile; a flag holding the
secret would sit in shell history and `ps`.

What is here is what an operator runs from a terminal: check the key (`me`),
declare the game's tables (`achievements define`, `stats define`,
`leaderboard define`), and read back (`entitlements`, `*-player`, `stats top`,
`leaderboard board`). What is left out is a game server's own job: see
docs/surfaces.md.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from typing import Any

from ..auth import IntegrationKeyCredential
from ..config import resolve_unkeyed
from ..context import Context
from ..errors import UsageError
from ..http import Transport

BASE = "/api/integration/v1"

#: Per-request ceilings from the docs; refused locally so a long file does not
#: spend a rate-limited call on a certain 400.
MAX_BOARDS = 25
MAX_STATS = 25
MAX_ACHIEVEMENTS = 500


def _key(args: Any) -> str:
    key_file = getattr(args, "key_file", None)

    if key_file:
        try:
            with open(key_file, "r", encoding="utf-8") as handle:
                key = handle.read().strip()
        except OSError as err:
            raise UsageError(f"Could not read the integration key: {err}") from None
    else:
        key = (os.environ.get("TMC_INTEGRATION_KEY") or "").strip()

    if not key:
        raise UsageError(
            "An integration key is required.",
            hint="Make one on the site (Account → Integrations), then set TMC_INTEGRATION_KEY or pass --key-file.",
        )

    if not key.startswith("tmci_"):
        raise UsageError(
            "That is not an integration key (they start with tmci_).",
            hint="The content key (tmc_…) is a different credential and is never sent to these routes.",
        )

    return key


def _transport(ctx: Context) -> Transport:
    settings = resolve_unkeyed(ctx.args)

    return Transport(
        settings.base_url,
        IntegrationKeyCredential(_key(ctx.args)),
        timeout=settings.timeout,
        retries=settings.retries,
        retry_wait_max=settings.retry_wait_max,
        verify_tls=settings.verify_tls,
        debug=settings.debug,
        dry_run=settings.dry_run,
    )


def _call(ctx: Context, method: str, path: str, *, params: dict[str, Any] | None = None, body: Any = None) -> Any:
    response = _transport(ctx).request(
        method,
        BASE + path,
        params={k: v for k, v in (params or {}).items() if v is not None},
        json_body=body,
    )

    return response.data


def _stamped(body: dict[str, Any]) -> dict[str, Any]:
    """The replay-protection pair every write accepts (`ts` seconds, a nonce)."""

    return {**body, "ts": int(time.time()), "nonce": secrets.token_hex(8)}


def _load_list(args: Any, noun: str, limit: int) -> list[Any]:
    """The table from `--file` (`-` = stdin): a bare array or `{<noun>: [...]}`."""

    path = args.file

    try:
        if path == "-":
            raw = sys.stdin.read()
        else:
            with open(path, "r", encoding="utf-8") as handle:
                raw = handle.read()
    except OSError as err:
        raise UsageError(f"Could not read {path}: {err}") from None

    try:
        data = json.loads(raw)
    except ValueError as err:
        raise UsageError(f"{path} is not JSON: {err}") from None

    if isinstance(data, dict):
        data = data.get(noun)

    if not isinstance(data, list) or not data:
        raise UsageError(f"{path} must be a non-empty JSON array of {noun} (or an object with a '{noun}' array).")

    if len(data) > limit:
        raise UsageError(f"{len(data)} {noun} in one request; the route takes at most {limit}.")

    return data


def _define(ctx: Context, route: str, noun: str, limit: int) -> int:
    items = _load_list(ctx.args, noun, limit)
    data = _call(ctx, "POST", route, body=_stamped({noun: items}))

    if ctx.dry_run:
        return 0

    ctx.emit(data)

    refused = data.get("refused") if isinstance(data, dict) else None

    if refused:
        ctx.note(f"{len(refused)} refused: {json.dumps(refused)}")

    return 0


def me(ctx: Context) -> int:
    ctx.emit(_call(ctx, "GET", "/me"))
    return 0


def introspect(ctx: Context) -> int:
    token = ctx.args.access_token

    if token == "-":
        token = sys.stdin.read().strip()

    if not token:
        raise UsageError("No access token given.")

    data = _call(ctx, "POST", "/player/introspect", body=_stamped({"access_token": token}))

    if ctx.dry_run:
        return 0

    ctx.emit(data)
    return 0


def entitlements(ctx: Context) -> int:
    ctx.emit(_call(ctx, "GET", "/store/entitlements", params={"player": ctx.args.player}))
    return 0


def achievements_define(ctx: Context) -> int:
    return _define(ctx, "/achievements/define", "achievements", MAX_ACHIEVEMENTS)


def achievements_player(ctx: Context) -> int:
    ctx.emit(_call(ctx, "GET", "/achievements/player", params={"player": ctx.args.player}))
    return 0


def stats_define(ctx: Context) -> int:
    return _define(ctx, "/stats/define", "stats", MAX_STATS)


def stats_player(ctx: Context) -> int:
    ctx.emit(_call(ctx, "GET", "/stats/player", params={"player": ctx.args.player}))
    return 0


def stats_top(ctx: Context) -> int:
    a = ctx.args
    ctx.emit(_call(ctx, "GET", "/stats/top", params={"stat": a.stat, "offset": a.offset, "limit": a.limit, "player": a.player}))
    return 0


def leaderboard_define(ctx: Context) -> int:
    return _define(ctx, "/leaderboard/define", "boards", MAX_BOARDS)


def leaderboard_board(ctx: Context) -> int:
    a = ctx.args
    ctx.emit(_call(ctx, "GET", "/leaderboard/board", params={
        "board": a.board, "scope": a.scope, "offset": a.offset, "limit": a.limit, "player": a.player,
    }))
    return 0
