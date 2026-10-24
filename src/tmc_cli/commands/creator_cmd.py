"""`tmc creator publish` — upload a GLB as a wearable, action or avatar.

`POST /api/avatar/v1/publish` in website-city (docs/ugc-wearables.md there): the
file is the raw request body, everything else is the query string, and the
credential is a creator token (`tmcc_…`), not the content key or an app-user
session. The site stores the file and hands it to the same Save function its
form calls, so limits, validation and the approval hold are the site's.

The token is read per call from `--token-file` or `TMC_CREATOR_TOKEN` and never
written to the profile; a flag holding the secret itself would sit in shell
history and `ps`.
"""

from __future__ import annotations

import os
from typing import Any

from ..auth import CreatorTokenCredential
from ..config import resolve_unkeyed
from ..context import Context
from ..errors import UsageError
from ..http import Transport
from .. import app_api

KINDS = ("wearable", "action", "avatar")

#: `WEARABLE_SLOTS` and `WEARABLE_LICENCES` in website-city's `src/types/avatar/wearables.ts`.
SLOTS = ("hat", "face", "neck", "back", "waist", "hand_right", "hand_left")
LICENCES = ("CC0-1.0", "CC-BY-4.0", "MIT", "OWN")

ACTION_KINDS = ("EMOTE", "POSE", "GESTURE")

SHARINGS = ("PRIVATE", "PUBLIC")


def _token(args: Any) -> str:
    if args.token_file:
        try:
            with open(args.token_file, "r", encoding="utf-8") as handle:
                token = handle.read().strip()
        except OSError as err:
            raise UsageError(f"Could not read the creator token: {err}") from None
    else:
        token = (os.environ.get("TMC_CREATOR_TOKEN") or "").strip()

    if not token:
        raise UsageError(
            "A creator token is required.",
            hint="Make one on the site (Avatars → creator tokens), then set TMC_CREATOR_TOKEN or pass --token-file.",
        )

    return token


def publish(ctx: Context) -> int:
    args = ctx.args

    # Checked here because each refusal spends one of the twenty uploads an hour
    # the route allows a token, and the route reads these only after the body.
    if args.kind == "wearable" and (not args.slot or not args.licence):
        raise UsageError("A wearable needs --slot and --licence.")

    try:
        with open(args.file, "rb") as handle:
            body = handle.read()
    except OSError as err:
        raise UsageError(f"Could not read {args.file}: {err}") from None

    if body[:4] != b"glTF":
        raise UsageError(f"{args.file} is not a binary glTF (.glb) file.")

    params: dict[str, Any] = {
        "kind": args.kind,
        "name": args.name,
        "description": args.description,
        "sharing": args.sharing,
        "id": args.id,
        "slot": args.slot,
        "licence": args.licence,
        "bone": args.bone,
        "credit": args.credit,
        "source": args.source,
        "actionKind": args.action_kind,
        "loop": "1" if args.loop else None,
    }

    settings = resolve_unkeyed(args)
    transport = Transport(
        settings.base_url,
        CreatorTokenCredential(_token(args)),
        timeout=settings.timeout,
        retries=settings.retries,
        retry_wait_max=settings.retry_wait_max,
        verify_tls=settings.verify_tls,
        debug=settings.debug,
        dry_run=settings.dry_run,
    )
    response = transport.request(
        "POST",
        "/api/avatar/v1/publish?" + app_api.query_string(params),
        raw_body=body,
        content_type="model/gltf-binary",
    )

    if ctx.dry_run:
        return 0

    data = response.data or {}
    ctx.emit(data)

    if not ctx.quiet and data.get("held"):
        ctx.note("Held for approval: it is not usable until a moderator passes it.")

    return 0
