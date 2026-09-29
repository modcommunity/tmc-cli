"""`tmc defcon …` — the network monitor's PUBLIC half, as the status page has it.

Defcon (`~/stack/defcon` for the node, `docs/defcon.md` in website-city for the
site) publishes exactly three things, all tRPC procedures under
`defcon.public.*`, all anonymous:

- `status` — the whole `/status` Defcon section: overall verdict, nodes, every
  public monitor with its per-node state and uptime, and the OPEN incidents.
- `series` — one public monitor's latency over a chart range, per node.
- `mtr` — one public MTR monitor's latest traceroute.

Nothing here reaches `defcon.admin.*`, and nothing should: that is the monitor
configuration, node hosts and full URLs, which the public procedures exist
precisely to leave out ("Nothing public names a node's host or a monitor's full
URL").

WHICH ORIGIN. Each procedure has a REST mirror on the API origin since
website-city b11efdfe/d83fb2bb (`GET /api/status/defcon`, `/series`, `/mtr`,
anonymous, the same objects in the app API's `{"ok", "data"}` envelope), so by
default these need only the base URL. A site that predates the mirrors answers
404 there, and then — or whenever `--site-url` is given — the tRPC procedures
are asked of the website origin instead (derived from the base URL:
`api.example.com` → `example.com`; tRPC is refused on the API container). The
site's transformer is superjson, so a tRPC answer is
`{"result": {"data": {"json": …}}}` and an input goes as `?input={"json": …}`.

WHAT IS NOT PUBLIC, AND SO NOT HERE: resolved incidents. `status` carries the
open ones (at most 20) and there is no public history — see the report in
CLAUDE.md, Known gaps, for the endpoint that would add it.

The site's own `show` switches are honoured: when the status page hides nodes
or incidents, so does this, even though `status` sends node rows regardless.
"""

from __future__ import annotations

import json
from typing import Any

from .. import output
from ..context import Context
from ..errors import ApiError, CliError, UsageError

class _DryRun(Exception):
    """Raised past a --dry-run fetch; `run` turns it into a clean exit."""


MONITOR_COLUMNS = ("id", "name", "kind", "target", "status", "avgMs24h", "up24h", "up7d", "up30d")

RANGES = ("day", "week", "month", "year", "all")

#: Worst first — `WorstStatus` in website-city orders them the same way.
STATUS_ORDER = ("DOWN", "DEGRADED", "UNKNOWN", "OK")

#: `tmc defcon status --check` exit codes: scripts want "is it up", not a table.
CHECK_EXIT = {"OK": 0, "DEGRADED": 1, "UNKNOWN": 3, "DOWN": 2}


#: The REST mirror of each `defcon.public.*` procedure, on the API origin.
REST = {
    "status": "/api/status/defcon",
    "series": "/api/status/defcon/series",
    "mtr": "/api/status/defcon/mtr",
}


def _call(ctx: Context, procedure: str, payload: dict[str, Any] | None = None) -> Any:
    if not getattr(ctx.args, "site_url", None):
        params = {k: v for k, v in (payload or {}).items() if v is not None}

        try:
            response = ctx.public_transport().request("GET", REST[procedure], params=params or None)
        except ApiError as err:
            # A site from before the mirrors: fall through to its tRPC.
            if err.status != 404:
                raise
        else:
            return None if ctx.dry_run else response.data

    return _call_trpc(ctx, procedure, payload)


def _call_trpc(ctx: Context, procedure: str, payload: dict[str, Any] | None = None) -> Any:
    params = {"input": json.dumps({"json": payload})} if payload is not None else None
    response = ctx.public_transport(site=True).request(
        "GET", f"/api/trpc/defcon.public.{procedure}", params=params
    )
    body = response.body

    if ctx.dry_run:
        return None

    try:
        return body["result"]["data"]["json"]
    except (TypeError, KeyError):
        err = ApiError(response.status, "The site did not answer in tRPC's shape.")
        err.hint = "Is --site-url the WEBSITE's origin? tRPC is refused on the API one."
        raise err from None


def _status(ctx: Context) -> dict[str, Any] | None:
    """The status document, or None on --dry-run (nothing was fetched)."""

    doc = _call(ctx, "status")

    if ctx.dry_run:
        return None

    if not isinstance(doc, dict):
        raise UsageError("The status page has no Defcon data (the site returned nothing).")

    return doc


def _published(doc: dict[str, Any] | None) -> dict[str, Any]:
    if doc is None:
        raise _DryRun()

    if not doc.get("enabled"):
        raise UsageError(
            "This site does not publish its network status.",
            hint="defcon.statusPublic is off on the site.",
        )

    return doc


def _node_names(doc: dict[str, Any]) -> dict[int, str]:
    return {n.get("id"): n.get("name") for n in doc.get("nodes") or []}


def _pct(value: Any) -> Any:
    return None if value is None else round(float(value), 3)


def _raw(ctx: Context) -> bool:
    return getattr(ctx.args, "output", "table") in ("json", "yaml")


def _find_monitor(doc: dict[str, Any], ref: str) -> dict[str, Any]:
    """A monitor by id, exact name, or a unique piece of its name."""

    monitors = doc.get("monitors") or []

    if ref.isdigit():
        for m in monitors:
            if m.get("id") == int(ref):
                return m

    lowered = ref.lower()
    exact = [m for m in monitors if str(m.get("name", "")).lower() == lowered]

    if exact:
        return exact[0]

    partial = [m for m in monitors if lowered in str(m.get("name", "")).lower()]

    if len(partial) == 1:
        return partial[0]

    if partial:
        raise UsageError(
            f"'{ref}' matches {len(partial)} monitors.",
            hint=", ".join(f"{m.get('id')}: {m.get('name')}" for m in partial[:10]),
        )

    raise UsageError(f"No public monitor '{ref}'.", hint="See 'tmc defcon monitors'.")


# ---- commands ----------------------------------------------------------------


def run(handler: Any) -> Any:
    """Wrap a command so --dry-run stops cleanly after printing the request."""

    def wrapped(ctx: Context) -> int:
        try:
            return handler(ctx)
        except _DryRun:
            return 0

    wrapped.__name__ = handler.__name__
    wrapped.__doc__ = handler.__doc__

    return wrapped


def status(ctx: Context) -> int:
    if not ctx.args.check:
        return _status_cmd(ctx)

    # Under --check the exit code IS the verdict, so a failure to get one must
    # not land on 1 or 2 (a usage error, a bad --site-url, an unpublished page)
    # and read as DEGRADED or DOWN. Anything that is not an answer is UNKNOWN.
    try:
        return _status_cmd(ctx)
    except CliError as err:
        output.error(f"error: {err.message}")

        if err.hint:
            output.error(f"  → {err.hint}")

        return CHECK_EXIT["UNKNOWN"]


def _status_cmd(ctx: Context) -> int:
    doc = _status(ctx)

    if doc is None:
        return 0

    if _raw(ctx):
        ctx.emit(doc)
    else:
        _published(doc)
        monitors = doc.get("monitors") or []
        counts = {s: sum(1 for m in monitors if m.get("status") == s) for s in STATUS_ORDER}

        ctx.emit(
            {
                "overall": doc.get("overall"),
                "monitors": len(monitors),
                **{s.lower(): counts[s] for s in STATUS_ORDER},
                "nodes": len(doc.get("nodes") or []) if (doc.get("show") or {}).get("nodes") else None,
                "openIncidents": len(doc.get("openAlerts") or [])
                if (doc.get("show") or {}).get("incidents")
                else None,
            }
        )

    if ctx.args.check:
        return CHECK_EXIT.get(str(doc.get("overall")), 3)

    return 0


def monitors(ctx: Context) -> int:
    doc = _published(_status(ctx))
    rows = doc.get("monitors") or []

    if ctx.args.status:
        wanted = {s.upper() for s in ctx.args.status}
        rows = [m for m in rows if m.get("status") in wanted]

    if ctx.args.kind:
        rows = [m for m in rows if m.get("kind") == ctx.args.kind.upper()]

    if _raw(ctx):
        ctx.emit(rows)
        return 0

    ctx.emit(
        [
            {
                "id": m.get("id"),
                "name": m.get("name"),
                "kind": m.get("kind"),
                "target": m.get("target"),
                "status": m.get("status"),
                "avgMs24h": m.get("dayAvgMs"),
                "up24h": _pct((m.get("uptime") or {}).get("day")),
                "up7d": _pct((m.get("uptime") or {}).get("week")),
                "up30d": _pct((m.get("uptime") or {}).get("month")),
            }
            for m in rows
        ],
        columns=MONITOR_COLUMNS,
    )

    return 0


def monitor(ctx: Context) -> int:
    """One monitor, and its current reading from every node."""

    doc = _published(_status(ctx))
    m = _find_monitor(doc, ctx.args.monitor)

    if _raw(ctx):
        ctx.emit(m)
        return 0

    names = _node_names(doc)
    show_nodes = (doc.get("show") or {}).get("nodes")

    ctx.note(
        f"{m.get('name')} · {m.get('kind')} {m.get('target')} · {m.get('status')}"
        f" · 24h avg {m.get('dayAvgMs')} ms"
    )
    ctx.emit(
        [
            {
                "node": names.get(s.get("nodeId"), s.get("nodeId")) if show_nodes else s.get("nodeId"),
                "status": s.get("status"),
                "latencyMs": s.get("lastValueMs"),
                "lossPct": s.get("lastLossPct"),
                "since": s.get("since"),
            }
            for s in m.get("nodes") or []
        ]
    )

    return 0


def nodes(ctx: Context) -> int:
    doc = _published(_status(ctx))

    if not (doc.get("show") or {}).get("nodes"):
        raise UsageError("This site does not publish its monitoring nodes.")

    rows = doc.get("nodes") or []

    ctx.emit(
        rows
        if _raw(ctx)
        else [
            {
                "id": n.get("id"),
                "name": n.get("name"),
                "location": n.get("location"),
                "status": n.get("status"),
                "lastSeen": n.get("lastSeen"),
                "down": n.get("down"),
            }
            for n in rows
        ]
    )

    return 0


def incidents(ctx: Context) -> int:
    """The OPEN incidents. Resolved ones are not public (see module doc)."""

    doc = _published(_status(ctx))

    if not (doc.get("show") or {}).get("incidents"):
        raise UsageError("This site does not publish its incidents.")

    rows = doc.get("openAlerts") or []

    ctx.emit(
        rows,
        columns=("id", "createdAt", "status", "monitorName", "nodeName", "message"),
    )

    if not ctx.quiet and not rows:
        ctx.note("No open incidents.")

    return 0


def latency(ctx: Context) -> int:
    """A monitor's latency series over a chart range, one row per node-bucket."""

    args = ctx.args
    monitor_id = int(args.monitor) if args.monitor.isdigit() else None

    if monitor_id is None:
        monitor_id = int(_find_monitor(_published(_status(ctx)), args.monitor)["id"])

    data = _call(ctx, "series", {"monitorId": monitor_id, "range": args.range})

    if data is None:
        return 0

    names = {n.get("id"): n.get("name") for n in data.get("nodes") or []}
    points = data.get("points") or []

    if args.node:
        wanted = args.node.lower()
        points = [
            p for p in points
            if str(p.get("nodeId")) == wanted or str(names.get(p.get("nodeId"), "")).lower() == wanted
        ]

    if args.summary:
        ctx.emit(_summarise(points, names))
        return 0

    if _raw(ctx):
        ctx.emit({**data, "points": points})
        return 0

    ctx.emit(
        [
            {
                "ts": p.get("ts"),
                "node": names.get(p.get("nodeId"), p.get("nodeId")),
                "avgMs": p.get("avg"),
                "maxMs": p.get("max"),
                "okPct": p.get("okPct"),
                "lossPct": p.get("lossPct"),
                "samples": p.get("samples"),
            }
            for p in points
        ]
    )

    if not ctx.quiet and not points:
        ctx.note("No samples in that range (or the monitor is not public).")

    return 0


def _summarise(points: list[dict[str, Any]], names: dict[Any, Any]) -> list[dict[str, Any]]:
    """Per node: the latest bucket's average, and the range's mean and peak."""

    by_node: dict[Any, list[dict[str, Any]]] = {}

    for p in points:
        by_node.setdefault(p.get("nodeId"), []).append(p)

    out = []

    for node_id, rows in by_node.items():
        rows = sorted(rows, key=lambda p: str(p.get("ts")))
        avgs = [float(p["avg"]) for p in rows if p.get("avg") is not None]
        peaks = [float(p["max"]) for p in rows if p.get("max") is not None]

        out.append(
            {
                "node": names.get(node_id, node_id),
                "latestMs": rows[-1].get("avg"),
                "meanMs": round(sum(avgs) / len(avgs), 3) if avgs else None,
                "peakMs": max(peaks) if peaks else None,
                "buckets": len(rows),
            }
        )

    return out


def mtr(ctx: Context) -> int:
    args = ctx.args
    monitor_id = int(args.monitor) if args.monitor.isdigit() else None

    if monitor_id is None:
        monitor_id = int(_find_monitor(_published(_status(ctx)), args.monitor)["id"])

    runs = _call(ctx, "mtr", {"monitorId": monitor_id})

    if runs is None:
        return 0

    if _raw(ctx):
        ctx.emit(runs)
        return 0

    if not runs:
        ctx.note("No traceroute published for that monitor (not MTR, not public, or MTR is hidden).")
        return 0

    rows = []

    for trace in runs if isinstance(runs, list) else [runs]:
        for hop in trace.get("hops") or []:
            rows.append({"node": trace.get("nodeId"), "ts": trace.get("ts"), **hop})

    ctx.emit(rows)

    return 0
