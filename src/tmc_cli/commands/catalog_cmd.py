"""`tmc catalog …` — the app API's public reads.

The content API (`/api/content`) is the WRITE side: it hands a key-holder their
own records and the anonymous caller a deliberately thin summary. What the site
shows a visitor — categories, tags, the owner, download and rating counts,
dependencies, the reviews under an item, the games catalogue, servers by address
— is served to the desktop app by `/api/app/v1`, and the read half of that is
open (`auth: 'optional'` in website-city's `handleAppApi`).

These commands send NO credential. A `Bearer` sent to the app API is resolved
as one of ITS tokens (`ResolveAppToken`), and a publishing credential is refused
there with `403 wrong_credential` — so presenting the profile's key would break
a read that needs no key at all. The per-user half
of that API (friends, parties, installs, subscriptions, writing reviews) takes
an app-user token instead — `appuser_cmd.py` and `app_api.py`.
"""

from __future__ import annotations

from typing import Any, Iterable

from .. import app_api
from ..context import Context
from ..errors import UsageError

#: `ContentKindVals` in website-city's `src/types/app-api/contract.ts`. Not the
#: content API's type list: `serverMap` and `user` are browsable, `group` is not.
KINDS = ("asset", "mod", "server", "serverMap", "article", "community", "collection", "user")

#: `BrowseSortVals`, the common ones plus the server-only ones.
SORTS = (
    "createdAt", "lastEdit", "name", "views", "downloads", "rating", "reviews",
    "favorites", "curUsers", "maxUsers", "avgUsers",
)

TIME_RANGES = ("all", "24h", "7d", "30d")

#: `AppBuildPlatformVals`: the machine asking; the route has no "all of them".
BUILD_PLATFORMS = (
    "WINDOWS_X64", "WINDOWS_ARM64", "LINUX_X64", "LINUX_ARM64",
    "MACOS_UNIVERSAL", "ANDROID_ARM64", "IOS_ARM64",
)

APP_TYPES = ("GAME", "GAME_SINGLEPLAYER", "GAME_ENGINE", "VOIP", "OTHER")

#: `normalizeArrayFields`: these are lists even with one value.
ARRAY_PARAMS = ("apps", "categories", "tags", "ids", "slugs", "countries", "adultAges")

#: The table's default; `--field` reaches the rest (webUrl, favorites, …).
SUMMARY_COLUMNS = ("kind", "id", "name", "app", "owner", "downloads", "rating", "categories", "tags")

#: `lookup` answers servers only, so the country is worth a column there.
SERVER_COLUMNS = ("kind", "id", "name", "app", "owner", "country", "categories", "tags")


def _get(ctx: Context, path: str, params: dict[str, Any]) -> Any:
    """GET with the app API's query dialect (`app_api.query_string`)."""

    return ctx.public_transport().request("GET", app_api.target(path, params)).data


def _name(ref: Any) -> Any:
    if isinstance(ref, dict):
        return ref.get("username") or ref.get("name") or ref.get("id")

    return ref


def _names(refs: Any) -> str:
    return ", ".join(str(_name(r)) for r in refs or [] if _name(r) is not None)


def _extra(pairs: Iterable[str] | None) -> dict[str, Any]:
    """`--filter key=value` — the long tail (server filters, mostly)."""

    out: dict[str, Any] = {}

    for pair in pairs or []:
        if "=" not in pair:
            raise UsageError(f"--filter expects key=value, got '{pair}'.")

        key, _, value = pair.partition("=")

        if key in ARRAY_PARAMS:
            out.setdefault(key, []).extend(v for v in value.split(",") if v)
        else:
            out[key] = value

    return out


def _summary_row(item: dict[str, Any]) -> dict[str, Any]:
    stats = item.get("stats") or {}
    server = item.get("server") or {}

    return {
        "kind": item.get("kind"),
        "id": item.get("id"),
        "name": item.get("name"),
        "app": _name(item.get("app")),
        "owner": _name(item.get("owner")),
        "downloads": stats.get("downloads"),
        "rating": stats.get("rating"),
        "reviews": stats.get("reviews"),
        "favorites": stats.get("favorites"),
        "categories": _names(item.get("categories")),
        "tags": _names(item.get("tags")),
        # Server rows only: the ISO code (`ServerInfo.countryCode`) where the
        # site has one, which is shorter in a table than the country's name.
        "country": server.get("countryCode") or server.get("country"),
        "webUrl": item.get("webUrl"),
    }


def _paged(ctx: Context, path: str, params: dict[str, Any], key: str) -> tuple[list[Any], Any, Any]:
    """Follow `nextCursor` when `--all`, up to `--max` rows."""

    args = ctx.args
    rows: list[Any] = []
    cursor = getattr(args, "cursor", None)
    page: Any = None
    cap = getattr(args, "max", None)

    while True:
        page = _get(ctx, path, {**params, "cursor": cursor})
        rows.extend((page or {}).get(key) or [])
        cursor = (page or {}).get("nextCursor")

        if not getattr(args, "all", False) or not cursor or (cap and len(rows) >= cap):
            break

    if cap:
        rows = rows[:cap]

    return rows, cursor, page


def _raw(ctx: Context) -> bool:
    return getattr(ctx.args, "output", "table") in ("json", "yaml")


def browse(ctx: Context) -> int:
    args = ctx.args
    params = {
        "kind": args.kind,
        "search": args.search,
        "apps": args.app,
        "categories": args.category,
        "tags": args.tag,
        "tagsOr": True if args.any_tag else None,
        "communityId": args.community,
        "ownerId": args.owner,
        "nsfw": args.nsfw,
        "isOfficial": True if args.official else None,
        "sort": args.sort,
        "sortDir": args.dir,
        "timeRange": args.time_range,
        "limit": args.limit,
        **_extra(args.filter),
    }

    rows, cursor, page = _paged(ctx, "/browse", params, "items")

    ctx.emit(rows if _raw(ctx) else [_summary_row(r) for r in rows], columns=SUMMARY_COLUMNS)

    if not ctx.quiet and getattr(args, "output", "table") == "table":
        total = (page or {}).get("total")
        tail = f" · next: --cursor {cursor}" if cursor else ""
        ctx.note(f"{len(rows)} shown" + (f" · {total} total" if total is not None else "") + tail)

    return 0


def show(ctx: Context) -> int:
    """One item as the site renders it, or one of its lists (`--part`)."""

    args = ctx.args
    detail = _get(ctx, f"/content/{args.kind}/{args.id}", {})

    if not isinstance(detail, dict):
        raise UsageError(f"{args.kind} {args.id} did not answer a record.")

    if args.part:
        part = detail.get(args.part) or []

        if args.part == "releases" and not _raw(ctx):
            part = [
                {
                    "id": r.get("id"),
                    "version": r.get("version"),
                    "createdAt": r.get("createdAt"),
                    "files": len(r.get("files") or []),
                }
                for r in part
            ]

        ctx.emit(part)
        return 0

    if _raw(ctx):
        ctx.emit(detail)
        return 0

    summary = detail.get("summary") or {}
    row = _summary_row(summary)
    row.update(
        {
            "releases": len(detail.get("releases") or []),
            "media": len(detail.get("media") or []),
            "links": len(detail.get("links") or []),
            "dependencies": len(detail.get("dependencies") or []),
        }
    )
    ctx.emit(row)

    return 0


def facets(ctx: Context) -> int:
    """What the filter sidebar offers for a kind — ids to feed `browse`.

    Counts use the browse list's own predicate, which for servers is "online"
    unless `online=false` is asked for (`--offline`), so a chip's number is the
    number of rows it selects.
    """

    params: dict[str, Any] = {"kind": ctx.args.kind}

    if getattr(ctx.args, "offline", False):
        params["online"] = False

    data = _get(ctx, "/facets", params) or {}

    if _raw(ctx):
        ctx.emit(data)
        return 0

    rows = []

    for facet, key in (("app", "apps"), ("category", "categories"), ("country", "countries"), ("map", "maps")):
        for ref in data.get(key) or []:
            rows.append(
                {
                    "facet": facet,
                    "id": ref.get("id"),
                    "name": ref.get("name"),
                    "count": ref.get("count"),
                    "parentId": ref.get("parentId"),
                    # The value a filter takes that is not the id: a country's
                    # ISO code, a map's own name (what `mapName` matches).
                    "code": ref.get("shortCode") or ref.get("key"),
                    "icon": ref.get("icon") or ref.get("image"),
                }
            )

    ctx.emit(rows, columns=("facet", "id", "name", "count", "parentId", "code"))

    return 0


def reviews(ctx: Context) -> int:
    args = ctx.args
    params = {"kind": args.kind, "id": args.id, "sort": args.sort, "limit": args.limit}

    rows, cursor, page = _paged(ctx, "/reviews", params, "reviews")

    if _raw(ctx):
        ctx.emit({**(page or {}), "reviews": rows})
        return 0

    ctx.emit(
        [
            {
                "id": r.get("id"),
                "owner": _name(r.get("owner")),
                "rating": r.get("rating"),
                "score": r.get("score"),
                "createdAt": r.get("createdAt"),
                "content": r.get("content"),
            }
            for r in rows
        ]
    )

    if not ctx.quiet and page:
        ctx.note(
            f"average {page.get('average')} over {page.get('total')} review(s)"
            + (f" · next: --cursor {cursor}" if cursor else "")
        )

    return 0


def games(ctx: Context) -> int:
    """The games catalogue (the site's `App` rows)."""

    args = ctx.args
    params = {
        "search": args.search,
        "type": args.type,
        "playable": True if args.playable else None,
        "ids": args.ids,
        "slugs": args.slug,
        "limit": args.limit,
    }

    rows, _cursor, _page = _paged(ctx, "/apps", params, "apps")

    if _raw(ctx):
        ctx.emit(rows)
        return 0

    ctx.emit(
        [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "slug": a.get("slug"),
                "type": a.get("type"),
                "isOfficial": a.get("isOfficial"),
                "hasServers": a.get("hasServers"),
                "webUrl": a.get("webUrl"),
            }
            for a in rows
        ]
    )

    return 0


def build(ctx: Context) -> int:
    """The one installable build of a game for a platform: `/apps/<id>/build`.

    The server answers `null` for every refusal (hidden game, disabled build,
    nothing for that platform), so an empty result is one fact, not several.
    """

    data = _get(ctx, f"/apps/{ctx.args.app}/build", {"platform": ctx.args.platform})

    if not data:
        ctx.note(f"no installable build of game {ctx.args.app} for {ctx.args.platform}")
        ctx.emit(None if _raw(ctx) else [])
        return 0

    ctx.emit(data)
    return 0


def lookup(ctx: Context) -> int:
    """Which listed servers answer at this address."""

    data = _get(ctx, "/servers/lookup", {"host": ctx.args.host, "port": ctx.args.port}) or {}
    servers = data.get("servers") or []

    ctx.emit(servers if _raw(ctx) else [_summary_row(s) for s in servers], columns=SERVER_COLUMNS)

    return 0


#: A listing row's columns: an address and nothing about a person.
LISTING_COLUMNS = ("id", "host", "port", "queryPort", "protocols", "app")


def listing(ctx: Context) -> int:
    """The address-only server listing game browsers read: `/api/content/server/list`.

    The content API's anonymous surface rather than the app API, so it is asked
    with no credential like the rest of `tmc catalog` and counts against the
    same per-address quota. Pages by number (`page`/`totalPages`), not cursor.
    """

    args = ctx.args
    params: dict[str, Any] = {"limit": args.limit}
    if args.app:
        params["appId" if str(args.app).isdigit() else "app"] = args.app

    rows: list[Any] = []
    page = args.page or 1
    pagination: dict[str, Any] | None = None

    while True:
        response = ctx.public_transport().request(
            "GET", "/api/content/server/list?" + app_api.query_string({**params, "page": page})
        )
        rows.extend(response.data or [])
        pagination = response.pagination
        pages = int((pagination or {}).get("totalPages") or 1)

        if not args.all or page >= pages:
            break

        page += 1

    if _raw(ctx):
        ctx.emit(rows, columns=LISTING_COLUMNS, pagination=None if args.all else pagination)
    else:
        ctx.emit(
            [
                {
                    **{k: r.get(k) for k in ("id", "host", "port", "queryPort")},
                    "protocols": ",".join(r.get("protocols") or []),
                    "app": (r.get("app") or {}).get("url"),
                }
                for r in rows
            ],
            columns=LISTING_COLUMNS,
            pagination=None if args.all else pagination,
        )

    return 0


def prefetch(ctx: Context) -> int:
    """What a client may download before joining a server: `/servers/<id>/prefetch`.

    The game's newest packs (its solo pack first), plus where the avatar library
    and the mirror list are. Public like the library it points at.
    """

    doc = _get(ctx, f"/servers/{ctx.args.server}/prefetch", {}) or {}

    if _raw(ctx):
        ctx.emit(doc)
        return 0

    ctx.emit(doc.get("packs") or [], columns=("id", "version", "bytes"))

    if not ctx.quiet:
        ctx.note(f"avatar library: {doc.get('avatar_library') or '-'} · mirrors: {doc.get('mirrors_url') or '-'}")

    return 0


def avatar_schema(ctx: Context) -> int:
    """The asset set the avatar editor draws (`/avatar/schema`, a `DotAvatarSchema`)."""

    ctx.emit(_get(ctx, "/avatar/schema", {}))

    return 0


#: `/api/avatar/v1/library` lists, and the columns a table shows for each.
LIBRARY_PARTS = {
    "avatars": ("id", "name", "source", "rig", "version", "size", "contentId"),
    "actions": ("id", "kind", "name", "builtin", "loop", "version", "size", "contentId"),
    "wearables": ("id", "name", "slot", "version", "size", "contentId"),
}


def avatar_library(ctx: Context) -> int:
    """Every uploaded avatar, action and wearable usable in a game or on a server.

    `/api/avatar/v1/library`, not the app API: a plain JSON body (no `ok`/`data`
    envelope) that game clients fetch before joining. `--server` narrows to what
    that server allows; `--app` is the game's whole set.
    """

    args = ctx.args
    params = {"server": args.server} if args.server else {"app": args.app}
    response = ctx.public_transport().request(
        "GET", "/api/avatar/v1/library?" + app_api.query_string(params)
    )
    doc = response.data or {}

    if _raw(ctx) and not args.part:
        ctx.emit(doc)
        return 0

    if args.part:
        ctx.emit(doc.get(args.part) or [], columns=LIBRARY_PARTS[args.part])
        return 0

    rows = [
        {"part": part, **{k: r.get(k) for k in ("id", "name", "version", "size", "contentId")}}
        for part in LIBRARY_PARTS
        for r in doc.get(part) or []
    ]
    ctx.emit(rows, columns=("part", "id", "name", "version", "size", "contentId"))

    return 0


def ticket_keys(ctx: Context) -> int:
    """The public keys game servers verify connect tickets with (`/auth/ticket-keys`).

    A read for an operator checking what their server will trust; minting a
    ticket (`auth/ticket`) is the joining client's and is not a command.
    """

    doc = _get(ctx, "/auth/ticket-keys", {}) or {}

    if _raw(ctx):
        ctx.emit(doc)
        return 0

    # `keys` already leaves the revoked ones out; `revoked` names them so a
    # server drops a cached copy. The PEMs are for `-o json`, not a table.
    rows = [{"kid": k.get("kid"), "alg": k.get("alg"), "revoked": False} for k in doc.get("keys") or []]
    rows += [{"kid": kid, "alg": None, "revoked": True} for kid in doc.get("revoked") or []]
    ctx.emit(rows, columns=("kid", "alg", "revoked"))

    if not ctx.quiet:
        ctx.note(f"issuer {doc.get('issuer')} · refresh every {doc.get('refreshSec')} s")

    return 0
