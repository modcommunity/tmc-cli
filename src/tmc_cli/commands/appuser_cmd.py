"""The app API as the signed-in member: device login, then friends, parties,
presence, installs, subscriptions, review writes, stats, account settings,
downloads, avatar and reports.

Every route here is website-city `src/app/api/app/v1/<path>/route.ts`, and every
body is spelled as that route's zod schema spells it (`src/types/app-api/
contract.ts`, plus `src/types/party/*` for the party inputs the routes reuse).
The session — storing, presenting, rotating the token — is `app_api.py`; this
module only shapes requests and rows.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import sys
import time
import webbrowser
from typing import Any

from .. import app_api, output
from ..app_api import AppSession
from ..context import Context
from ..errors import ApiError, CliError, UsageError
from ..version import __version__

#: `ContentKindVals` (reviews) and `SubKindVals` (subscriptions, install items).
REVIEW_KINDS = ("asset", "mod", "server", "serverMap", "article", "community", "collection", "user")
SUB_KINDS = ("asset", "mod", "collection")
#: `PresenceStatusVals`.
PRESENCE_STATUSES = ("offline", "online", "in_game", "away")
#: `PartyTypeVals`.
PARTY_TYPES = ("PUBLIC", "PUBLIC_PASSWORD", "FRIENDS", "PRIVATE")
#: `PartySetMemberRoleInput.role`; `HOST` is `party transfer`, not a role change.
PARTY_ROLES = {"co-host": "CO_HOST", "member": "MEMBER"}
#: `ReportKindVals`.
REPORT_TYPES = ("SPAM", "OTHER")
#: `UserSettingsSchema`: two strings, the rest switches. PATCH takes any subset.
SETTING_STRINGS = {"timezone": 64, "locale": 16}
SETTING_SWITCHES = (
    "emailNotifications", "pushNotifications", "notifySystem", "notifyComments", "notifyReviews",
    "notifyReleases", "notifyMentions", "notifyModeration", "notifyCredits", "notifyFriends",
    "notifyFollowers", "notifyMessages", "notifyStatus", "notifyParties", "notifyGroups",
)
#: `DeployStrategyVals` / `SandboxEnvVals`.
STRATEGIES = ("direct", "hardlink", "symlink", "usvfs")
ENVIRONMENTS = ("client", "server", "shared")

#: The device poll's codes (`DEVICE_*` in the contract).
PENDING = "authorization_pending"
SLOW_DOWN = "slow_down"
DENIED = "access_denied"
EXPIRED = "expired_token"

#: RFC 8628 §3.5: on `slow_down`, add five seconds to the interval for good.
SLOW_DOWN_STEP_SEC = 5

#: Indirection so tests can drive the poll without waiting on it.
_sleep = time.sleep


def _session(ctx: Context) -> AppSession:
    return AppSession(ctx.args)


def _raw(ctx: Context) -> bool:
    return getattr(ctx.args, "output", "table") in ("json", "yaml")


def _platform() -> str:
    """`ClientInfoSchema.platform` — a label for the approval screen."""

    if sys.platform.startswith("win"):
        return "windows"

    if sys.platform == "darwin":
        return "macos"

    if sys.platform.startswith("linux"):
        return "linux"

    return "unknown"


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge). The server accepts S256 only (`VerifyPkce`).

    48 random bytes is a 64-character verifier — inside the 43..128 the schema
    allows on both ends.
    """

    verifier = _b64u(os.urandom(48))
    challenge = _b64u(hashlib.sha256(verifier.encode("ascii")).digest())

    return verifier, challenge


# ---- login / logout / me ----------------------------------------------------


def login(ctx: Context) -> int:
    """The device flow: open a grant, show the code, poll until it is approved."""

    args = ctx.args
    transport = app_api.anonymous_transport(args)
    base_url = transport.base_url
    verifier, challenge = pkce_pair()

    host = socket.gethostname() or "unknown host"
    client = {
        # Shown to the member on the approval page and on Account → Devices.
        "name": f"tmc CLI on {host}"[:64],
        "platform": _platform(),
        "version": __version__[:32],
    }

    start = app_api.anonymous_post(
        transport, "/auth/device", {"client": client, "codeChallenge": challenge}
    ).data

    if ctx.dry_run or not isinstance(start, dict):
        return 0

    url = start.get("verificationUriComplete") or start.get("verificationUri")

    # Not through ctx.note: under --quiet the login would otherwise wait on an
    # approval nobody was told how to give.
    output.info(f"Open {url}")
    output.info(f"and confirm the code: {start.get('userCode')}")

    if getattr(args, "browser", False) and url:
        webbrowser.open(url)

    token = _poll(
        transport,
        device_code=str(start["deviceCode"]),
        verifier=verifier,
        interval=float(start.get("interval") or 5),
        expires_in=float(start.get("expiresIn") or 600),
    )

    name, path = app_api.store_session(args, base_url, token)
    user = token.get("user") or {}

    ctx.done(f"Signed in as {user.get('username') or user.get('name') or user.get('id')} → profile '{name}' ({path})")

    return 0


def _poll(transport: Any, *, device_code: str, verifier: str, interval: float, expires_in: float) -> dict[str, Any]:
    """`auth/token` until it answers with a token pair.

    Pending and slow-down are `202` with the error envelope — a success status,
    so they arrive as a Response rather than an ApiError. Denied and expired
    are `400`. Time is counted from the waits themselves rather than the clock,
    so the deadline is the grant's `expiresIn` however slow each request is…
    plus those requests, which only ever errs towards stopping early.
    """

    waited = 0.0

    while True:
        if waited >= expires_in:
            raise CliError("The login expired before it was approved.", hint=app_api.LOGIN_HINT)

        _sleep(interval)
        waited += interval

        try:
            resp = app_api.anonymous_post(
                transport, "/auth/token", {"deviceCode": device_code, "codeVerifier": verifier}
            )
        except ApiError as err:
            if err.code == DENIED:
                raise CliError("The login was declined in the browser.") from None

            if err.code == EXPIRED:
                raise CliError("The login expired before it was approved.", hint=app_api.LOGIN_HINT) from None

            raise

        body = resp.body if isinstance(resp.body, dict) else {}

        if resp.status == 202 or body.get("ok") is False:
            if body.get("code") == SLOW_DOWN:
                retry = body.get("retryAfter")
                interval = max(interval + SLOW_DOWN_STEP_SEC, float(retry or 0))

            continue

        token = resp.data

        if not isinstance(token, dict) or "accessToken" not in token:
            raise CliError("The site answered the login poll with no token.")

        return token


def logout(ctx: Context) -> int:
    """Revoke the session's whole lineage server-side, then forget it locally."""

    try:
        session = _session(ctx)
    except CliError as err:
        ctx.note(err.message)
        return 0

    if session.session is None:
        raise UsageError("TMC_APP_TOKEN is set; there is no stored session to sign out.")

    try:
        session.request("POST", "/auth/revoke", json_body={})
    except CliError as err:
        # Forgotten either way: a session the server already ended is still one
        # the user asked to be rid of.
        message = err.message if not isinstance(err, ApiError) else f"HTTP {err.status}: {err.message}"
        output.warn(f"Could not revoke on the server ({message}); forgetting it locally.")

    if not ctx.dry_run:
        app_api.forget_session(ctx.args)

    ctx.done("Signed out of the app session.")

    return 0


def me(ctx: Context) -> int:
    session = _session(ctx)
    data = session.request("GET", "/me") or {}

    if _raw(ctx):
        ctx.emit(data)
        return 0

    user = data.get("user") or {}
    row = {
        "id": user.get("id"),
        "username": user.get("username"),
        "name": user.get("name"),
        "role": user.get("role"),
        "site": session.settings.base_url,
    }

    if session.session is not None:
        row["tokenExpires"] = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(session.session.get("expires_at") or 0)
        )

    ctx.emit(row)

    return 0


# ---- friends ----------------------------------------------------------------


def _presence_row(p: dict[str, Any] | None) -> dict[str, Any]:
    p = p or {}

    return {
        "status": p.get("status"),
        "app": p.get("appName"),
        "server": p.get("serverName"),
        "partyId": p.get("partyId"),
        "joinable": p.get("joinable"),
        "detail": p.get("detail"),
    }


def friends_list(ctx: Context) -> int:
    rows = _session(ctx).request("GET", "/friends") or []

    if _raw(ctx):
        ctx.emit(rows)
        return 0

    ctx.emit(
        [{"userId": f.get("userId"), "displayName": f.get("displayName"), **_presence_row(f.get("presence"))} for f in rows],
        columns=("userId", "displayName", "status", "app", "server", "detail"),
    )

    return 0


def friends_requests(ctx: Context) -> int:
    data = _session(ctx).request("GET", "/friends/requests") or {}

    if _raw(ctx):
        ctx.emit(data)
        return 0

    rows = [
        {"direction": direction, **r}
        for direction in ("incoming", "outgoing")
        for r in data.get(direction) or []
    ]
    ctx.emit(rows, columns=("direction", "id", "userId", "displayName", "createdAt"))

    return 0


def friends_add(ctx: Context) -> int:
    data = _session(ctx).request("POST", "/friends/request", json_body={"userId": ctx.args.user_id})
    ctx.emit(data)
    ctx.done(f"Friend request sent to {ctx.args.user_id}.")

    return 0


def friends_respond(accept: bool):
    def run(ctx: Context) -> int:
        _session(ctx).request(
            "POST", "/friends/respond", json_body={"requestId": ctx.args.request_id, "accept": accept}
        )
        ctx.done(f"Request {ctx.args.request_id} {'accepted' if accept else 'declined'}.")

        return 0

    return run


def friends_cancel(ctx: Context) -> int:
    _session(ctx).request("POST", "/friends/cancel", json_body={"requestId": ctx.args.request_id})
    ctx.done(f"Request {ctx.args.request_id} withdrawn.")

    return 0


def friends_remove(ctx: Context) -> int:
    if not ctx.confirm(f"Remove {ctx.args.user_id} from your friends?"):
        return 1

    _session(ctx).request("POST", "/friends/remove", json_body={"userId": ctx.args.user_id})
    ctx.done(f"Removed {ctx.args.user_id}.")

    return 0


# ---- presence ---------------------------------------------------------------


def presence_get(ctx: Context) -> int:
    # ONE comma-separated string (`z.object({ userIds: z.string() })`), not the
    # repeated keys the rest of this API takes: a repeated key would reach the
    # schema as an array and be refused.
    ids = ",".join(ctx.args.user_ids)
    data = _session(ctx).request("GET", "/presence", params={"userIds": ids}) or {}

    if _raw(ctx):
        ctx.emit(data)
        return 0

    ctx.emit(
        [{"userId": uid, **_presence_row(p)} for uid, p in data.items()],
        columns=("userId", "status", "app", "server", "partyId", "joinable", "detail"),
    )

    # Left out, not answered offline: the route only speaks about friends.
    missing = [u for u in ctx.args.user_ids if u not in data]

    if missing:
        ctx.note(f"Not answered (not a friend, or unknown): {', '.join(missing)}")

    return 0


def presence_set(ctx: Context) -> int:
    args = ctx.args
    body: dict[str, Any] = {"status": args.status}

    if args.server is not None:
        body["serverId"] = args.server

    if args.party is not None:
        body["partyId"] = args.party

    if args.joinable:
        body["joinable"] = True

    if args.detail is not None:
        body["detail"] = args.detail

    data = _session(ctx).request("POST", "/presence", json_body=body)
    ctx.emit(data)

    return 0


# ---- parties ----------------------------------------------------------------

PARTY_COLUMNS = ("id", "name", "type", "stage", "users", "maxUsers", "appId", "serverId", "hostId")


def _party_out(ctx: Context, party: Any) -> None:
    if party is None:
        if _raw(ctx):
            ctx.emit(None)
        else:
            ctx.note("Not in a party.")
        return

    if _raw(ctx) or not isinstance(party, dict):
        ctx.emit(party)
    elif getattr(ctx.args, "members", False):
        ctx.emit(party.get("members") or [], columns=("userId", "displayName", "role", "state", "presence", "readyAt"))
    else:
        ctx.emit({k: party.get(k) for k in PARTY_COLUMNS})


def party_mine(ctx: Context) -> int:
    _party_out(ctx, _session(ctx).request("GET", "/party/mine"))

    return 0


def party_show(ctx: Context) -> int:
    _party_out(ctx, _session(ctx).request("GET", f"/party/{ctx.args.id}"))

    return 0


def party_create(ctx: Context) -> int:
    args = ctx.args
    body: dict[str, Any] = {"appId": args.app}

    for key, value in (
        ("name", args.name),
        ("description", args.description),
        ("type", args.type),
        ("maxUsers", args.max_users),
        ("serverId", args.server),
        ("password", args.password),
    ):
        if value is not None:
            body[key] = value

    party = _session(ctx).request("POST", "/party/create", json_body=body)
    _party_out(ctx, party)

    return 0


def party_invite(ctx: Context) -> int:
    args = ctx.args
    body: dict[str, Any] = {"id": args.id, "userIds": args.user_ids}

    if args.message is not None:
        body["message"] = args.message

    ids = _session(ctx).request("POST", "/party/invite", json_body=body) or []
    ctx.emit([{"inviteId": i} for i in ids] if not _raw(ctx) else ids)

    return 0


def party_invites(ctx: Context) -> int:
    rows = _session(ctx).request("GET", "/party/invites") or []
    ctx.emit(rows, columns=("id", "partyId", "partyName", "inviterId", "message"))

    return 0


def party_respond(accept: bool):
    def run(ctx: Context) -> int:
        data = _session(ctx).request(
            "POST", "/party/invite/respond", json_body={"inviteId": ctx.args.invite_id, "accept": accept}
        )

        if data is not None:
            ctx.emit(data)

        ctx.done(f"Invite {ctx.args.invite_id} {'accepted' if accept else 'declined'}.")

        return 0

    return run


def party_join(ctx: Context) -> int:
    body: dict[str, Any] = {"id": ctx.args.id}

    if ctx.args.password is not None:
        body["password"] = ctx.args.password

    ctx.emit(_session(ctx).request("POST", "/party/join", json_body=body))

    return 0


def party_leave(ctx: Context) -> int:
    body: dict[str, Any] = {"id": ctx.args.id}

    if ctx.args.silent:
        body["silent"] = True

    ctx.emit(_session(ctx).request("POST", "/party/leave", json_body=body))

    return 0


def party_kick(ctx: Context) -> int:
    body = {"id": ctx.args.id, "userId": ctx.args.user_id, "ban": bool(ctx.args.ban)}
    _session(ctx).request("POST", "/party/kick", json_body=body)
    ctx.done(f"{'Banned' if ctx.args.ban else 'Kicked'} {ctx.args.user_id}.")

    return 0


def _ready_out(ctx: Context, view: Any) -> None:
    """`PartyReadyViewT`, as one summary row unless asked for the whole thing."""

    if _raw(ctx) or not isinstance(view, dict):
        ctx.emit(view)
        return

    me = view.get("me") or {}
    ctx.emit({
        "stage": view.get("stage"),
        "ready": view.get("ready"),
        "total": view.get("total"),
        "pct": view.get("pct"),
        "countdownSec": view.get("countdownSec"),
        "started": view.get("started"),
        "meReady": me.get("ready"),
    })


def party_ready(ctx: Context) -> int:
    body = {"id": ctx.args.id, "ready": not ctx.args.not_ready}
    _ready_out(ctx, _session(ctx).request("POST", "/party/ready", json_body=body))

    return 0


def party_start(ctx: Context) -> int:
    body: dict[str, Any] = {"id": ctx.args.id}

    if ctx.args.server is not None:
        body["serverId"] = ctx.args.server

    _ready_out(ctx, _session(ctx).request("POST", "/party/start", json_body=body))

    return 0


def party_round(ctx: Context) -> int:
    # `GET party/{id}/ready`: a GET that may fire a lapsed auto-start, exactly as
    # the website's `ready.state` does; idempotent, so harmless to repeat.
    _ready_out(ctx, _session(ctx).request("GET", f"/party/{ctx.args.id}/ready"))

    return 0


def party_manage(route: str, done: str, *, ask: str | None = None):
    """`{ id }` → `null` routes for a party's managers: cancel, end, force-start."""

    def run(ctx: Context) -> int:
        pid = ctx.args.id

        if ask and not ctx.confirm(ask.format(id=pid)):
            return 1

        _session(ctx).request("POST", f"/party/{route}", json_body={"id": pid})
        ctx.done(done.format(id=pid))

        return 0

    return run


def party_role(ctx: Context) -> int:
    args = ctx.args
    body = {"id": args.id, "userId": args.user_id, "role": PARTY_ROLES[args.role]}
    _session(ctx).request("POST", "/party/role", json_body=body)
    ctx.done(f"{args.user_id} is now {args.role} of party {args.id}.")

    return 0


def party_transfer(ctx: Context) -> int:
    args = ctx.args

    if not ctx.confirm(f"Hand party {args.id} to {args.user_id}? You stop being its host."):
        return 1

    _session(ctx).request("POST", "/party/transfer", json_body={"id": args.id, "userId": args.user_id})
    ctx.done(f"{args.user_id} hosts party {args.id} now.")

    return 0


RESERVATION_COLUMNS = ("id", "status", "private", "startsAt", "endsAt", "partyId", "server", "note")


def _reservation_out(ctx: Context, row: Any) -> None:
    if _raw(ctx) or not isinstance(row, dict):
        ctx.emit(row)
        return

    server = row.get("server") or {}
    ctx.emit({**{k: row.get(k) for k in RESERVATION_COLUMNS}, "server": server.get("name") or server.get("id")})


def party_reserve(ctx: Context) -> int:
    args = ctx.args
    body: dict[str, Any] = {"partyId": args.id, "serverId": args.server, "minutes": args.minutes, "private": bool(args.private)}

    if args.note is not None:
        body["note"] = args.note

    row = _session(ctx).request("POST", "/party/reserve", json_body=body)
    _reservation_out(ctx, row)

    if isinstance(row, dict) and row.get("status") == "PENDING":
        ctx.note("Pending: the server's owner approves bookings by hand.")

    return 0


def party_reserve_cancel(ctx: Context) -> int:
    if not ctx.confirm(f"Cancel reservation {ctx.args.reservation_id}?"):
        return 1

    row = _session(ctx).request("POST", "/party/reserve/cancel", json_body={"reservationId": ctx.args.reservation_id})
    _reservation_out(ctx, row)

    return 0


def party_offer(ctx: Context) -> int:
    data = _session(ctx).request("GET", f"/party/{ctx.args.id}/reserve/offer", params={"serverId": ctx.args.server})
    ctx.emit(data)

    return 0


# ---- account: settings, downloads, avatar -----------------------------------


def account_settings(ctx: Context) -> int:
    data = _session(ctx).request("GET", "/me") or {}
    settings = data.get("settings") or {}

    ctx.emit(settings if _raw(ctx) else [{"key": k, "value": v} for k, v in settings.items()], columns=("key", "value"))

    return 0


def _setting(pair: str) -> tuple[str, Any]:
    key, sep, value = pair.partition("=")
    key = key.strip()

    if not sep:
        raise UsageError(f"Expected key=value, got '{pair}'.")

    if key in SETTING_STRINGS:
        if len(value) > SETTING_STRINGS[key]:
            raise UsageError(f"{key} is at most {SETTING_STRINGS[key]} characters.")
        return key, value

    if key in SETTING_SWITCHES:
        word = value.strip().lower()

        if word in ("true", "on", "yes", "1"):
            return key, True

        if word in ("false", "off", "no", "0"):
            return key, False

        raise UsageError(f"{key} is a switch: true or false.")

    raise UsageError(
        f"Unknown setting '{key}'.",
        hint="One of: " + ", ".join([*SETTING_STRINGS, *SETTING_SWITCHES]),
    )


def account_set(ctx: Context) -> int:
    # A partial, as the route wants: only what is named here is sent, so another
    # device's change to a different switch is not overwritten with a stale one.
    body = dict(_setting(pair) for pair in ctx.args.pairs)
    settings = _session(ctx).request("PATCH", "/me", json_body=body) or {}

    ctx.emit(settings if _raw(ctx) else [{"key": k, "value": settings.get(k)} for k in body], columns=("key", "value"))

    return 0


DOWNLOAD_COLUMNS = ("deviceName", "isThisDevice", "active", "queued", "paused", "failed", "finished", "speedBps", "remainingBytes", "updatedAt")


def account_downloads(ctx: Context) -> int:
    data = _session(ctx).request("GET", "/downloads") or {}

    if _raw(ctx):
        ctx.emit(data)
        return 0

    devices = data.get("devices") or []

    if getattr(ctx.args, "items", False):
        ctx.emit(
            [{"deviceName": d.get("deviceName"), **i} for d in devices for i in d.get("items") or [] if isinstance(i, dict)],
        )
    else:
        ctx.emit([{k: d.get(k) for k in DOWNLOAD_COLUMNS} for d in devices], columns=DOWNLOAD_COLUMNS)

    return 0


def account_avatar(ctx: Context) -> int:
    ctx.emit(_session(ctx).request("GET", "/avatar"))

    return 0


def account_avatar_set(ctx: Context) -> int:
    path = ctx.args.file

    try:
        if path == "-":
            doc = json.load(sys.stdin)
        else:
            with open(path, "r", encoding="utf-8") as handle:
                doc = json.load(handle)
    except (OSError, ValueError) as err:
        raise UsageError(f"Could not read an avatar document from {path}: {err}") from None

    if not isinstance(doc, dict):
        raise UsageError("An avatar document is a JSON object (what `tmc account avatar -o json` prints).")

    ctx.emit(_session(ctx).request("PUT", "/avatar", json_body=doc))

    return 0


# ---- report -----------------------------------------------------------------


def report(ctx: Context) -> int:
    args = ctx.args
    content = args.content

    if args.content_file:
        with open(args.content_file, "r", encoding="utf-8") as handle:
            content = handle.read()

    # `ReportCreateRequest`: title 3..120, content 10..4000. Said here rather than
    # spending one of the ten reports an hour the route allows on a refusal.
    if not 3 <= len(args.title) <= 120:
        raise UsageError("--title is 3 to 120 characters.")

    if not content or not 10 <= len(content) <= 4000:
        raise UsageError("--content is 10 to 4000 characters.")

    body = {"kind": args.kind, "id": args.id, "type": args.type, "title": args.title, "content": content}
    ctx.emit(_session(ctx).request("POST", "/report", json_body=body))
    ctx.done(f"Reported {args.kind} {args.id}.")

    return 0


# ---- play center ------------------------------------------------------------

PLAY_TABS = ("games", "servers", "lobbies", "invites")
PLAY_MODES = ("web", "app")
#: `PlaySortVals`; which tab takes which is the site's `PlayTabSorts`, so a
#: sort the tab cannot express is the server's refusal to report, not ours.
PLAY_SORTS = ("players", "recent", "name", "servers", "popular", "trending", "newest")

PLAY_COLUMNS = {
    "games": ("id", "name", "modes", "serverCnt", "playerCnt", "lobbyCnt", "directPlay", "solo", "isOfficial"),
    "servers": ("id", "name", "appName", "modes", "curUsers", "maxUsers", "map", "online"),
    "lobbies": ("id", "name", "appName", "hostName", "stage", "users", "maxUsers", "needsPassword"),
    "invites": ("id", "name", "appName", "hostName", "stage", "users", "maxUsers", "needsPassword"),
}


def _play_row(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "modes": ",".join(row.get("modes") or [])} if "modes" in row else row


def play_page(ctx: Context) -> int:
    """One page of a play-center tab (`GET /play/page`).

    Sent as the signed-in member: the route takes a token or none, and the
    lobbies and invites tabs are only meaningful with one (friends' lobbies,
    the party). `data: null` is the site saying the play center is switched off.
    """

    args = ctx.args
    params = {
        "tab": args.tab,
        "q": args.search,
        "appId": args.app,
        "mode": args.mode,
        "sort": args.sort,
        "populated": True if args.populated else None,
        "joinable": True if args.joinable else None,
    }
    cursor = args.cursor
    rows: list[Any] = []

    while True:
        page = _session(ctx).request("GET", "/play/page", params={**params, "cursor": cursor})

        if page is None:
            ctx.note("the play center is switched off on this site")
            ctx.emit(None if _raw(ctx) else [])
            return 0

        key = "apps" if page.get("tab") == "games" else page.get("tab")
        rows.extend(page.get(key) or [])
        cursor = page.get("nextCursor")

        if not args.all or cursor is None or (args.max and len(rows) >= args.max):
            break

    if args.max:
        rows = rows[: args.max]

    if _raw(ctx):
        ctx.emit({**page, key: rows})
        return 0

    ctx.emit([_play_row(r) for r in rows], columns=PLAY_COLUMNS[args.tab])

    if not ctx.quiet:
        totals = page.get("totals") or {}
        ctx.note(
            f"{len(rows)} shown · {totals.get('games')} game(s), {totals.get('servers')} server(s), "
            f"{totals.get('players')} player(s), {totals.get('lobbies')} lobb(ies)"
            + (f" · next: --cursor {cursor}" if cursor is not None else "")
        )

    return 0


def play_playable(ctx: Context) -> int:
    """Which games carry a Play button (`GET /play/playable`); `enabled: false` is the off answer."""

    data = _session(ctx).request("GET", "/play/playable") or {}

    if _raw(ctx):
        ctx.emit(data)
        return 0

    ids = lambda key: ",".join(str(i) for i in data.get(key) or [])  # noqa: E731
    ctx.emit([{"enabled": data.get("enabled"), "appIds": ids("appIds"), "directIds": ids("directIds"), "soloIds": ids("soloIds")}])

    return 0


QUICK_COLUMNS = ("id", "name", "curUsers", "maxUsers", "map", "countryCode", "score", "why", "viaRotation")


def play_quick_match(ctx: Context) -> int:
    """The best server for a game right now (`POST /play/quick-match`); writes nothing.

    `data: null` is "play center off, or the game is not listed".
    """

    body = {"appId": ctx.args.app, "rotation": bool(ctx.args.rotation)}
    data = _session(ctx).request("POST", "/play/quick-match", json_body=body)

    if not data:
        ctx.note(f"no quick match for game {ctx.args.app}: the play center is off or the game is not listed")
        ctx.emit(None if _raw(ctx) else [])
        return 0

    if _raw(ctx):
        ctx.emit(data)
        return 0

    ctx.emit(
        [{**c, "why": ", ".join(c.get("why") or [])} for c in data.get("top") or []],
        columns=QUICK_COLUMNS,
    )
    best = data.get("best")
    ctx.note(
        f"{data.get('appName')} · scanned {data.get('scanned')}, full {data.get('full')}, stale {data.get('stale')}"
        + (f" · best: {best.get('name')} (#{best.get('id')})" if best else " · no server to join")
    )

    return 0


# ---- installs ---------------------------------------------------------------

INSTALL_COLUMNS = ("id", "app", "name", "isDefault", "strategy", "environment", "items", "playSeconds", "lastPlayedAt")


def _install_row(i: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": i.get("id"),
        "app": (i.get("app") or {}).get("name"),
        "name": i.get("name"),
        "isDefault": i.get("isDefault"),
        "strategy": i.get("strategy"),
        "environment": i.get("environment"),
        "items": len(i.get("items") or []),
        "playSeconds": i.get("playSeconds"),
        "lastPlayedAt": i.get("lastPlayedAt"),
        "gameVersion": i.get("gameVersion"),
        "loader": i.get("loader"),
    }


def _install_out(ctx: Context, install: Any) -> None:
    if _raw(ctx) or not isinstance(install, dict):
        ctx.emit(install)
    elif getattr(ctx.args, "items", False):
        ctx.emit(install.get("items") or [], columns=("kind", "itemId", "name", "enabled", "order", "subscribed"))
    else:
        ctx.emit(_install_row(install))


def installs_list(ctx: Context) -> int:
    data = _session(ctx).request("GET", "/installs", params={"appId": ctx.args.app}) or {}
    rows = data.get("installs") or []

    ctx.emit(rows if _raw(ctx) else [_install_row(i) for i in rows], columns=INSTALL_COLUMNS)

    return 0


def _install_fields(args: Any) -> dict[str, Any]:
    body: dict[str, Any] = {}

    for key, value in (
        ("name", args.name),
        ("description", args.description),
        ("gameVersion", args.game_version),
        ("loader", args.loader),
        ("strategy", args.strategy),
        ("environment", args.environment),
    ):
        if value is not None:
            body[key] = value

    if args.default:
        body["isDefault"] = True

    return body


def installs_create(ctx: Context) -> int:
    body = {"appId": ctx.args.app, **_install_fields(ctx.args)}
    _install_out(ctx, _session(ctx).request("POST", "/installs", json_body=body))

    return 0


def installs_update(ctx: Context) -> int:
    fields = _install_fields(ctx.args)

    if not fields:
        raise UsageError("Nothing to change.", hint="Pass --name, --default, --loader, …")

    _install_out(ctx, _session(ctx).request("PATCH", "/installs", json_body={"id": ctx.args.id, **fields}))

    return 0


def installs_delete(ctx: Context) -> int:
    if not ctx.confirm(f"Delete install {ctx.args.id}?"):
        return 1

    _session(ctx).request("DELETE", "/installs", json_body={"id": ctx.args.id})
    ctx.done(f"Deleted install {ctx.args.id}.")

    return 0


def install_item(method: str):
    """`installs/items`: POST adds, PATCH sets enabled/order, DELETE removes."""

    def run(ctx: Context) -> int:
        args = ctx.args
        body: dict[str, Any] = {"installId": args.install_id, "kind": args.kind, "itemId": args.item_id}

        if method == "PATCH":
            if args.enabled is not None:
                body["enabled"] = args.enabled

            if args.order is not None:
                body["order"] = args.order

            if len(body) == 3:
                raise UsageError("Nothing to change.", hint="Pass --enable/--disable or --order N.")

        install = _session(ctx).request(method, "/installs/items", json_body=body)

        args.items = True
        _install_out(ctx, install)

        return 0

    return run


# ---- subscriptions ----------------------------------------------------------

SUB_COLUMNS = ("id", "kind", "itemId", "name", "app", "version", "autoUpdate", "notifyUpdates", "paused", "installable")


def subs_list(ctx: Context) -> int:
    params = {"since": ctx.args.since, "limit": ctx.args.limit}
    data = _session(ctx).request("GET", "/subscriptions", params=params) or {}

    if _raw(ctx):
        ctx.emit(data)
        return 0

    rows = [
        {
            **{k: s.get(k) for k in ("id", "kind", "itemId", "name", "autoUpdate", "notifyUpdates", "paused", "installable", "webUrl")},
            "app": (s.get("app") or {}).get("name"),
            "version": (s.get("release") or {}).get("version"),
        }
        for s in data.get("items") or []
    ]
    ctx.emit(rows, columns=SUB_COLUMNS)
    ctx.note(f"revision {data.get('revision')} ({'full' if data.get('full') else 'changes since --since'})")

    return 0


def subs_set(subscribed: bool):
    def run(ctx: Context) -> int:
        body = {"kind": ctx.args.kind, "itemId": ctx.args.item_id, "subscribed": subscribed}
        ctx.emit(_session(ctx).request("POST", "/subscriptions", json_body=body))

        return 0

    return run


def subs_prefs(ctx: Context) -> int:
    args = ctx.args
    body: dict[str, Any] = {"kind": args.kind, "itemId": args.item_id}

    for key, value in (("autoUpdate", args.auto_update), ("notifyUpdates", args.notify), ("paused", args.paused)):
        if value is not None:
            body[key] = value

    if len(body) == 2:
        raise UsageError("Nothing to change.", hint="Pass --auto-update/--no-auto-update, --notify/--no-notify or --paused/--no-paused.")

    _session(ctx).request("POST", "/subscriptions/prefs", json_body=body)
    ctx.done(f"Updated {args.kind} {args.item_id}.")

    return 0


# ---- reviews ----------------------------------------------------------------


def review_write(ctx: Context) -> int:
    args = ctx.args
    content = args.content

    if args.content_file:
        with open(args.content_file, "r", encoding="utf-8") as handle:
            content = handle.read()

    # The schema refuses neither-nor; saying so here saves a round trip.
    if args.rating is None and not (content or "").strip():
        raise UsageError("Give it a --rating, some --content, or both.")

    body: dict[str, Any] = {"kind": args.kind, "id": args.id}

    if args.rating is not None:
        body["rating"] = args.rating

    if content is not None:
        body["content"] = content

    data = _session(ctx).request("POST", "/reviews/write", json_body=body)
    ctx.emit(data)

    if isinstance(data, dict) and data.get("held"):
        ctx.note("Held for approval: only you can see it until then.")

    return 0


def review_delete(ctx: Context) -> int:
    if not ctx.confirm(f"Delete review {ctx.args.id}?"):
        return 1

    _session(ctx).request("DELETE", "/reviews/write", json_body={"id": ctx.args.id})
    ctx.done(f"Deleted review {ctx.args.id}.")

    return 0


def review_vote(ctx: Context) -> int:
    positive = {"up": True, "down": False, "clear": None}[ctx.args.vote]
    ctx.emit(_session(ctx).request("PUT", "/reviews/write", json_body={"id": ctx.args.id, "positive": positive}))

    return 0


# ---- stats ------------------------------------------------------------------


def stats_top(ctx: Context) -> int:
    args = ctx.args
    params = {"app": args.app, "stat": args.stat, "offset": args.offset, "limit": args.limit}
    data = _session(ctx).request("GET", "/stats/top", params=params) or {}

    if _raw(ctx):
        ctx.emit(data)
        return 0

    ctx.emit(data.get("rows") or [], columns=("rank", "player", "name", "value"))

    stat = data.get("stat") or {}
    mine = data.get("self")
    ctx.note(
        f"{stat.get('name') or stat.get('key')} · {stat.get('players')} player(s)"
        + (f" · you: #{mine.get('rank')} ({mine.get('value')})" if mine else " · you: no value")
    )

    return 0


def stats_me(ctx: Context) -> int:
    data = _session(ctx).request("GET", "/stats/me", params={"app": ctx.args.app}) or {}

    if _raw(ctx):
        ctx.emit(data)
        return 0

    ctx.emit(data.get("stats") or [], columns=("key", "name", "kind", "unit", "value"))

    return 0


def stats_defs(ctx: Context) -> int:
    rows = _session(ctx).request("GET", "/stats/defs", params={"app": ctx.args.app}) or []
    ctx.emit(rows, columns=("key", "name", "kind", "unit", "players", "description"))

    return 0
