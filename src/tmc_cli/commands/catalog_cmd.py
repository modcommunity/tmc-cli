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
an app-user token this tool has no way to hold — see CLAUDE.md, Known gaps.
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Iterable

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

APP_TYPES = ("GAME", "GAME_SINGLEPLAYER", "GAME_ENGINE", "VOIP", "OTHER")

#: `normalizeArrayFields`: these are lists even with one value.
ARRAY_PARAMS = ("apps", "categories", "tags", "ids", "slugs", "countries", "adultAges")

API = "/api/app/v1"

#: The table's default; `--field` reaches the rest (webUrl, favorites, …).
SUMMARY_COLUMNS = ("kind", "id", "name", "app", "owner", "downloads", "rating", "categories", "tags")


def _get(ctx: Context, path: str, params: dict[str, Any]) -> Any:
    """GET with the app API's query dialect: a list is a REPEATED key.

    `encode_params` joins lists with commas, which the content API splits and
    the app API does not — `parseQuery` there takes `?tags=1,2` as the one
    string "1,2" and the schema then refuses it.
    """

    pairs: list[tuple[str, str]] = []

    for key, value in params.items():
        if value is None or value == [] or value == "":
            continue

        if isinstance(value, bool):
            pairs.append((key, "true" if value else "false"))
        elif isinstance(value, (list, tuple)):
            pairs.extend((key, str(v)) for v in value)
        else:
            pairs.append((key, str(value)))

    query = urllib.parse.urlencode(pairs)
    target = f"{API}{path}" + (f"?{query}" if query else "")

    return ctx.public_transport().request("GET", target).data


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
    """What the filter sidebar offers for a kind — ids to feed `browse`."""

    data = _get(ctx, "/facets", {"kind": ctx.args.kind}) or {}

    if _raw(ctx):
        ctx.emit(data)
        return 0

    rows = []

    for facet, key in (("app", "apps"), ("category", "categories"), ("country", "countries")):
        for ref in data.get(key) or []:
            rows.append(
                {
                    "facet": facet,
                    "id": ref.get("id"),
                    "name": ref.get("name"),
                    "count": ref.get("count"),
                    "parentId": ref.get("parentId"),
                }
            )

    ctx.emit(rows, columns=("facet", "id", "name", "count", "parentId"))

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


def lookup(ctx: Context) -> int:
    """Which listed servers answer at this address."""

    data = _get(ctx, "/servers/lookup", {"host": ctx.args.host, "port": ctx.args.port}) or {}
    servers = data.get("servers") or []

    ctx.emit(servers if _raw(ctx) else [_summary_row(s) for s in servers], columns=SUMMARY_COLUMNS)

    return 0
