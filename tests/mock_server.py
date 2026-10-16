"""An in-memory stand-in for `/api/content`, faithful where it matters.

Not a re-implementation of the site — a model of the behaviours this CLI has
opinions about, so those opinions can be tested without a database:

  - both credential shapes, told apart by syntax exactly as the real auth does,
    including Ed25519 signature verification and one-shot `jti` consumption;
  - the `{ data }` / `{ data, pagination }` envelopes and the `{ error, issues,
    index }` error shape;
  - relation semantics: PUT replaces the set, POST merges by identity, DELETE
    with no body clears;
  - the bulk caps (25 write / 100 delete) and the 20-part upload cap, so the
    CLI's batching is exercised rather than assumed;
  - a rate-limit trip switch, to drive the retry path.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from tmc_cli.ed25519 import _verify_pure  # noqa: E402

BEARER_TOKEN = "tmc_" + "a1" * 12

RELATION_PARENTS = {
    # `group` included: relations.ts lists it under tags, and it is the one
    # relation a group has.
    "tags": {"asset", "mod", "server", "community", "article", "collection", "group"},
    "media": {"asset", "mod", "server", "community", "article"},
    "releases": {"asset", "mod", "server"},
    "links": {"mod", "server", "community"},
    "sources": {"asset", "mod"},
    "items": {"collection"},
}

REQUIRED = {
    "asset": ["name"],
    "mod": ["name", "content", "appId"],
    "server": ["appId"],
    "community": ["name"],
    "article": ["title", "content"],
    "collection": ["name"],
    "group": ["name"],
    "tag": ["name"],
    "release": ["version"],
    "comment": ["content"],
}

KNOWN_TYPES = set(REQUIRED) | {"favorite", "filter", "review", "media"}

#: The seven content items, and the only types the unauthenticated surface
#: answers for (`ANON_TYPES` in anon.ts).
ANON_TYPES = {"asset", "mod", "server", "community", "article", "collection", "group"}

#: Types whose anonymous listing understands `?appId=` (`HAS_APP`).
ANON_APP_TYPES = {"asset", "mod", "server", "article"}

#: Types whose anonymous listing understands `?official=` (`HAS_OFFICIAL`).
ANON_OFFICIAL_TYPES = {"asset", "mod", "server", "article"}

ANON_LIST_LIMIT = 20

ANON_LIST_NOTE = (
    "Anonymous listings are summaries. Fetch an item by id for engagement "
    "counts, or use an API key for the full record and its relations."
)


def default_defcon() -> dict[str, Any]:
    return {
        "enabled": True,
        "show": {"nodes": True, "incidents": True, "mtr": True},
        "overall": "DEGRADED",
        "nodes": [
            {"id": 1, "name": "Frankfurt", "location": "DE", "status": "OK", "lastSeen": "2026-09-27T11:00:00Z", "down": False},
            {"id": 2, "name": "Sydney", "location": "AU", "status": "DEGRADED", "lastSeen": "2026-09-27T11:00:00Z", "down": False},
        ],
        "monitors": [
            {"id": 1, "name": "Homepage", "kind": "WEB", "target": "example.com/", "status": "DEGRADED",
             "nodes": [
                 {"nodeId": 1, "status": "OK", "lastValueMs": 44.0, "lastLossPct": None, "since": "2026-09-27T09:00:00Z"},
                 {"nodeId": 2, "status": "DEGRADED", "lastValueMs": 820.5, "lastLossPct": None, "since": "2026-09-27T10:30:00Z"},
             ],
             "uptime": {"day": 99.5, "week": 99.9, "month": 99.95}, "dayAvgMs": 80.2},
            {"id": 2, "name": "API", "kind": "TCP", "target": "api.example.com:443", "status": "OK",
             "nodes": [], "uptime": {"day": 100, "week": 100, "month": 100}, "dayAvgMs": 12.0},
            {"id": 3, "name": "Route to EU", "kind": "MTR", "target": "example.com", "status": "OK",
             "nodes": [], "uptime": {"day": None, "week": None, "month": None}, "dayAvgMs": None},
        ],
        "openAlerts": [
            {"id": 40, "createdAt": "2026-09-27T10:30:00Z", "status": "DEGRADED", "monitorName": "Homepage", "nodeName": "Sydney", "message": "820 ms > 500 ms"},
        ],
    }


def app_summary(n: int) -> dict[str, Any]:
    """A `ContentSummarySchema` row. Ids are STRINGS on this surface."""

    kind = "server" if n == 5 else "mod"

    return {
        "kind": kind, "id": str(n), "url": None, "name": f"Item {n}", "description": None,
        "images": {"card": None, "banner": None, "icon": None},
        "app": {"id": 1, "name": "Seed", "url": "seed", "icon": None},
        "owner": {"id": "u1", "name": "Ann", "username": "ann", "avatar": None},
        "community": None,
        "categories": [{"id": 7, "name": "Maps", "url": None}],
        "tags": [{"id": 1, "name": "fun", "url": None}] + ([{"id": 2, "name": "pvp", "url": None}] if n % 2 else []),
        "createdAt": "2026-01-01", "updatedAt": None, "nsfw": False, "archived": False, "isOfficial": False,
        "stats": {"views": 1, "downloads": 10 * n, "favorites": 0, "comments": 0, "reviews": 1, "likes": 0, "rating": 4.5},
        "server": None, "webUrl": f"https://example.com/seed/m/{n}",
    }


class State:
    """Everything the mock remembers, resettable between tests."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.rows: dict[str, dict[int, dict[str, Any]]] = {t: {} for t in KNOWN_TYPES}
        self.relations: dict[tuple[str, int, str], list[Any]] = {}
        self.files: dict[str, dict[str, Any]] = {}
        self.next_id = 1
        self.seen_jti: set[str] = set()
        self.requests: list[tuple[str, str]] = []
        #: The Authorization header of each request, or None when there was
        #: none. The anonymous surface's contract is the ABSENCE of the header,
        #: so a test has to be able to assert on it rather than infer it.
        self.auth_headers: list[str | None] = []
        #: Public keys by key id, for JWT auth.
        self.public_keys: dict[str, bytes] = {}
        #: When > 0, the next N requests answer 429.
        self.rate_limit_for = 0
        #: The site-wide `api.anon.enabled` switch.
        self.anon_enabled = True
        #: When > 0, the next N anonymous requests answer 429 with retry-after.
        self.anon_rate_limit_for = 0
        self.can_read = True
        self.can_write = True
        self.can_delete = True
        #: `defcon.public.status` as the site computes it (`GetDefconPublicStatus`).
        self.defcon = default_defcon()
        #: `defcon.public.series` per monitor id, and `defcon.public.mtr`.
        self.defcon_series: dict[int, dict[str, Any]] = {
            1: {
                "points": [
                    {"ts": "2026-09-27T10:00:00Z", "nodeId": 1, "avg": 40.0, "max": 55.0, "okPct": 100, "lossPct": None, "samples": 12},
                    {"ts": "2026-09-27T11:00:00Z", "nodeId": 1, "avg": 44.0, "max": 90.0, "okPct": 100, "lossPct": None, "samples": 12},
                    {"ts": "2026-09-27T10:00:00Z", "nodeId": 2, "avg": 120.0, "max": 130.0, "okPct": 100, "lossPct": None, "samples": 12},
                ],
                "buckets": ["2026-09-27T10:00:00Z", "2026-09-27T11:00:00Z"],
                "nodes": [{"id": 1, "name": "Frankfurt"}, {"id": 2, "name": "Sydney"}],
            }
        }
        #: False plays a site from before `GET /api/status/defcon`: those 404.
        self.defcon_rest = True
        #: `defcon.public.incidents`, newest first; paged by id like the site.
        self.defcon_history: list[dict[str, Any]] = [
            {"id": 300 - n, "createdAt": f"2026-09-{27 - n % 20:02d}T10:00:00Z",
             "resolvedAt": None if n == 0 else f"2026-09-{27 - n % 20:02d}T11:00:00Z",
             "status": "DOWN" if n % 3 else "DEGRADED", "monitorName": "Homepage",
             "nodeName": "Frankfurt", "message": f"incident {n}"}
            for n in range(130)
        ]
        self.defcon_mtr: dict[int, list[Any]] = {
            3: [{"nodeId": 1, "ts": "2026-09-27T11:00:00Z", "hops": [
                {"hop": 1, "addr": "10.0.0.1", "name": "gw", "sent": 10, "recv": 10, "lossPct": 0, "bestMs": 1, "avgMs": 1.2, "worstMs": 2, "stdevMs": 0.1},
            ]}]
        }
        #: The app API's public reads, keyed by what they answer for.
        self.catalog_items = [app_summary(n) for n in range(1, 6)]
        self.app = AppUserState()

    def take_id(self) -> int:
        value = self.next_id
        self.next_id += 1

        return value



class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- plumbing ------------------------------------------------------------

    def log_message(self, *args: Any) -> None:  # silence the test output
        pass

    def _json(
        self, status: int, body: Any, headers: dict[str, str] | None = None
    ) -> None:
        payload = json.dumps(body).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))

        for name, value in (headers or {}).items():
            self.send_header(name, value)

        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status: int, message: str, **extra: Any) -> None:
        self._json(status, {"error": message, **extra})

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)

        return self.rfile.read(length) if length else b""

    def _read_json(self) -> Any:
        raw = self._read_body()

        if not raw:
            return None

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    # -- auth ----------------------------------------------------------------

    def _authenticate(self) -> str | None:
        """Returns an error message, or None when the credential is good."""

        header = self.headers.get("Authorization")

        if not header:
            return "Missing API token. Provide an Authorization header."

        token = re.sub(r"^bearer\s+", "", header.strip(), flags=re.IGNORECASE)

        # Shape decides the path, before any lookup — same rule as the server.
        if token.count(".") == 2:
            return self._authenticate_jwt(token)

        if token != BEARER_TOKEN:
            return "Invalid API token."

        return None

    def _authenticate_jwt(self, token: str) -> str | None:
        import base64

        def b64u(part: str) -> bytes:
            return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

        head_raw, body_raw, sig_raw = token.split(".")

        try:
            header = json.loads(b64u(head_raw))
            claims = json.loads(b64u(body_raw))
            signature = b64u(sig_raw)
        except Exception:
            return "Invalid or expired API assertion."

        if header.get("alg") != "EdDSA":
            return "Invalid or expired API assertion."

        key_id = header.get("kid")
        public = STATE.public_keys.get(key_id or "")

        if not public or claims.get("iss") != key_id:
            return "Invalid or expired API assertion."

        signing_input = f"{head_raw}.{body_raw}".encode("ascii")

        if not _verify_pure(public, signing_input, signature):
            return "Invalid or expired API assertion."

        now = int(time.time())

        if claims["exp"] <= now - 300 or claims["iat"] > now + 300:
            return "Invalid or expired API assertion."

        if claims["exp"] - claims["iat"] > 300:
            return "Invalid or expired API assertion."

        jti = claims.get("jti")

        if not jti or jti in STATE.seen_jti:
            return "This assertion has already been used."

        STATE.seen_jti.add(jti)

        return None

    def _permitted(self, method: str) -> bool:
        if method == "GET":
            return STATE.can_read

        if method == "DELETE":
            return STATE.can_delete

        return STATE.can_write

    # -- dispatch ------------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}

        STATE.requests.append((method, self.path))
        STATE.auth_headers.append(self.headers.get("Authorization"))

        if STATE.rate_limit_for > 0:
            STATE.rate_limit_for -= 1
            self._read_body()
            self._error(429, "Rate limit exceeded. Try again in 1s.")
            return

        # The app API and tRPC: public reads, answered only WITHOUT a credential
        # — a content key sent to the app API is refused as the wrong credential,
        # which is exactly what `tmc catalog` must avoid.
        if parts == ["api", "status", "summary"]:
            self._read_body()
            self._json(200, {"ok": True, "data": {
                "level": "minor", "open": [{"id": 1}], "upcoming": [],
                "core": {"reporting": True, "lastReportAt": "2026-10-16T07:00:00Z", "checks": [], "probes": []},
            }})
            return

        if parts[:3] == ["api", "status", "defcon"]:
            self._read_body()
            self._defcon_rest(parts[3:], query)
            return

        if parts[:3] == ["api", "app", "v1"] and parts[3:4] and (
            parts[3] in APP_USER_ROOTS or parts[3:5] == ["reviews", "write"]
        ):
            body = self._read_json() if method != "GET" else None
            _app_user(self, method, "/".join(parts[3:]), parse_qs(parsed.query), body)
            return

        if parts[:3] == ["api", "app", "v1"] or parts[:2] == ["api", "trpc"]:
            self._read_body()

            if self.headers.get("Authorization"):
                self._json(403, {"ok": False, "code": "wrong_credential", "message": "This endpoint cannot be called with a publishing credential."})
                return

            if parts[1] == "trpc":
                self._trpc(parts[2] if len(parts) > 2 else "", parsed.query)
            else:
                self._app_api(parts[3:], parse_qs(parsed.query))
            return

        # The address-only server listing (website-city `/api/content/server/list`):
        # anonymous, paged by number, one row per address.
        if method == "GET" and parts == ["api", "content", "server", "list"]:
            self._read_body()
            app = query.get("app") or query.get("appId")
            if app not in (None, "seed", "1"):
                self._json(404, {"error": "No such game.", "code": "unknown_app"})
                return
            rows = [
                {"id": 703 + n, "host": "203.0.113.%d" % (7 + n), "port": 6060, "queryPort": None,
                 "protocols": ["A2S"], "app": {"id": 1, "url": "seed"}}
                for n in range(3)
            ]
            page = int(query.get("page") or 1)
            limit = int(query.get("limit") or 2)
            self._json(200, {
                "data": rows[(page - 1) * limit:page * limit],
                "pagination": {"page": page, "limit": limit, "total": len(rows), "totalPages": -(-len(rows) // limit)},
            })
            return

        # A GET with NO Authorization header at all is the unauthenticated
        # surface's, and it is branched to BEFORE auth — a request presenting a
        # credential, even a bad one, is never downgraded into an anonymous read.
        if (
            method == "GET"
            and not self.headers.get("Authorization")
            and parts[:2] == ["api", "content"]
            and parts[2:3] not in ([], ["file"])
        ):
            self._read_body()
            self._anon(parts[2:], query)
            return

        failure = self._authenticate()

        if failure is not None:
            self._read_body()
            status = 400 if "already been used" in failure else 401
            self._error(status, failure)
            return

        if not self._permitted(method):
            self._read_body()
            self._error(403, f"This key lacks permission for {method} requests.")
            return

        if parts[:2] != ["api", "content"]:
            self._read_body()
            self._error(404, "Not found.")
            return

        rest = parts[2:]

        if rest and rest[0] == "file":
            self._files(method, rest[1:], query)
            return

        if len(rest) == 1:
            self._collection(method, rest[0], query)
        elif len(rest) == 2:
            self._item(method, rest[0], rest[1])
        elif len(rest) == 3:
            self._relation(method, rest[0], rest[1], rest[2])
        else:
            self._read_body()
            self._error(404, "Not found.")

    # -- tRPC (defcon.public.*) and the app API ------------------------------

    def _trpc(self, procedure: str, raw_query: str) -> None:
        query = parse_qs(raw_query)
        payload = json.loads(query["input"][0])["json"] if "input" in query else None

        def ok(value: Any) -> None:
            self._json(200, {"result": {"data": {"json": value}}})

        if procedure == "defcon.public.status":
            return ok(STATE.defcon)

        if procedure == "defcon.public.series":
            if payload.get("range") not in (None, "day", "week", "month", "year", "all"):
                return self._json(400, {"error": {"json": {"message": "Invalid input", "data": {"code": "BAD_REQUEST"}}}})

            return ok(STATE.defcon_series.get(payload["monitorId"], {"points": [], "buckets": [], "nodes": []}))

        if procedure == "defcon.public.mtr":
            return ok(STATE.defcon_mtr.get(payload["monitorId"], []))

        self._json(404, {"error": {"json": {"message": f'No "query"-procedure on path "{procedure}"', "data": {"code": "NOT_FOUND"}}}})

    def _defcon_rest(self, rest: list[str], query: dict[str, str]) -> None:
        """`GET /api/status/defcon[/series|/mtr]`: the same objects, app-API envelope."""

        def ok(value: Any) -> None:
            self._json(200, {"ok": True, "data": value})

        if not STATE.defcon_rest:
            return self._json(404, {"ok": False, "code": "not_found", "message": "Not found."})

        if rest == []:
            return ok(STATE.defcon)

        monitor = int(query.get("monitorId", "0") or 0)

        if rest == ["series"]:
            if query.get("range") not in (None, "day", "week", "month", "year", "all"):
                return self._json(400, {"ok": False, "code": "invalid", "message": "Invalid enum value"})

            return ok(STATE.defcon_series.get(monitor, {"points": [], "buckets": [], "nodes": []}))

        if rest == ["mtr"]:
            return ok(STATE.defcon_mtr.get(monitor, []))

        if rest == ["incidents"]:
            if not (STATE.defcon.get("enabled") and (STATE.defcon.get("show") or {}).get("incidents")):
                return ok({"enabled": False, "items": [], "nextCursor": None})

            cursor = int(query.get("cursor", "0") or 0)
            limit = int(query.get("limit", "25"))
            rows = [r for r in STATE.defcon_history if not cursor or r["id"] < cursor]
            page = rows[:limit]
            more = len(rows) > limit
            return ok({"enabled": True, "items": page, "nextCursor": page[-1]["id"] if more else None})

        self._json(404, {"ok": False, "code": "not_found", "message": "Not found."})

    def _app_api(self, rest: list[str], query: dict[str, list[str]]) -> None:
        def ok(data: Any) -> None:
            self._json(200, {"ok": True, "data": data})

        def one(key: str) -> str | None:
            return query.get(key, [None])[-1]

        route = "/".join(rest)

        if route == "browse":
            # Repeated keys, never commas: the real `parseQuery` would take
            # "1,2" as one string and the schema would refuse it.
            if any("," in v for values in query.values() for v in values):
                return self._json(400, {"ok": False, "code": "BAD_REQUEST", "message": "Expected number, received string"})

            items = [i for i in STATE.catalog_items if i["kind"] == one("kind")]

            if "tags" in query:
                wanted = {int(t) for t in query["tags"]}
                items = [i for i in items if wanted <= {t["id"] for t in i["tags"]}]

            limit = int(one("limit") or 30)
            start = int(one("cursor") or 0)
            page = items[start:start + limit]
            nxt = str(start + limit) if start + limit < len(items) else None

            return ok({"items": page, "nextCursor": nxt, "total": len(items)})

        if len(rest) == 3 and rest[0] == "content":
            for item in STATE.catalog_items:
                if item["kind"] == rest[1] and item["id"] == rest[2]:
                    return ok({
                        "summary": item,
                        "content": "body",
                        "rules": None,
                        "releases": [{"id": 9, "version": "1.0", "createdAt": "2026-01-01", "files": [{"id": "f", "title": None, "size": 3, "url": "u"}]}],
                        "media": [],
                        "links": [],
                        "dependencies": [{"relation": "REQUIRED", "note": None, "kind": "mod", "id": 2, "name": "Lib", "icon": None}],
                    })

            return self._json(404, {"ok": False, "code": "NOT_FOUND", "message": "Not found."})

        if route == "facets":
            return ok({
                "apps": [{"id": 1, "name": "Seed", "url": "seed", "count": 5, "icon": None}],
                "categories": [{"id": 7, "name": "Maps", "url": None, "count": 2, "parentId": None}],
                "countries": [],
            })

        if route == "reviews":
            return ok({
                "reviews": [{"id": 1, "owner": {"id": "u1", "name": "Ann", "username": "ann", "avatar": None}, "rating": 5, "content": "great", "createdAt": "2026-01-01", "lastEdit": None, "score": 2, "mine": False, "myVote": None}],
                "nextCursor": None,
                "breakdown": {"one": 0, "two": 0, "three": 0, "four": 0, "five": 1},
                "average": 5,
                "total": 1,
            })

        if route == "apps":
            return ok({"apps": [{"id": 1, "name": "Seed", "slug": "seed", "type": "GAME", "isOfficial": True, "hasServers": True, "webUrl": "https://x/seed"}], "nextCursor": None, "total": 1, "playEnabled": True})

        if route == "servers/lookup":
            if not one("host"):
                return self._json(400, {"ok": False, "code": "BAD_REQUEST", "message": "host is required"})

            return ok({"servers": [i for i in STATE.catalog_items if i["kind"] == "server"]})

        self._json(404, {"ok": False, "code": "NOT_FOUND", "message": "Not found."})

    # -- the unauthenticated surface -----------------------------------------

    def _anon_fail(self, status: int, code: str, message: str, **headers: str) -> None:
        """Every anonymous refusal carries a machine-readable `code`."""

        self._json(
            status,
            {"error": message, "code": code, "apiKeys": "/account/api"},
            headers or None,
        )

    def _anon(self, rest: list[str], query: dict[str, str]) -> None:
        # Order matters, and it is the server's: the site switch, then whether
        # the type has a surface at all, then budget — only then a lookup.
        if not STATE.anon_enabled:
            self._anon_fail(401, "auth_required", "This endpoint requires an API key.")
            return

        type_name = rest[0] if rest else ""

        if type_name not in ANON_TYPES:
            if type_name in KNOWN_TYPES:
                self._anon_fail(
                    401,
                    "auth_required",
                    f"Reading '{type_name}' requires an API key.",
                )
            else:
                self._anon_fail(
                    404, "unknown_type", f"Unknown content type '{type_name}'."
                )
            return

        if STATE.anon_rate_limit_for > 0:
            STATE.anon_rate_limit_for -= 1
            self._anon_fail(
                429,
                "rate_limited",
                "Rate limit exceeded for unauthenticated requests. Try again in 1s.",
                # The anonymous surface sets this; the keyed one never has.
                **{"retry-after": "1"},
            )
            return

        if len(rest) >= 2:
            self._anon_one(type_name, rest[1])
            return

        self._anon_list(type_name, query)

    def _anon_one(self, type_name: str, raw_id: str) -> None:
        try:
            item_id = int(raw_id)
        except ValueError:
            self._anon_fail(404, "not_found", "Not found, or not publicly readable.")
            return

        row = STATE.rows[type_name].get(item_id)

        # Hidden and NSFW are indistinguishable from a nonexistent id on purpose
        # — the alternative enumerates unpublished work.
        if row is None or row.get("hidden") or row.get("nsfw"):
            self._anon_fail(404, "not_found", "Not found, or not publicly readable.")
            return

        # `apiPublic` is the one refusal that says so: the item is plainly public
        # on the website, so confirming it exists leaks nothing.
        if row.get("apiPublic") is False:
            self._anon_fail(
                403,
                "api_disabled",
                "The owner of this item has turned off unauthenticated API access to it.",
            )
            return

        summary = _anon_summary(type_name, row)
        summary["stats"] = {
            "views": 0,
            "favorites": 0,
            "likes": 0,
            "dislikes": 0,
            "comments": 0,
        }

        self._json(200, {"data": summary}, {"x-cache": "MISS"})

    def _anon_list(self, type_name: str, query: dict[str, str]) -> None:
        rows = [
            row
            for row in sorted(
                STATE.rows[type_name].values(), key=lambda r: r["id"], reverse=True
            )
            if not row.get("hidden")
            and not row.get("nsfw")
            and row.get("apiPublic") is not False
        ]

        # `appId` is one of the two filters, and only on the types whose model
        # has it.
        if query.get("appId") and type_name in ANON_APP_TYPES:
            rows = [r for r in rows if r.get("appId") == int(query["appId"])]

        # `official` is the other. The server reads anything but `0`/`false`/`no`
        # as true, so the mock has to as well — a client that sent Python's
        # `False` would otherwise be filtering for the opposite of what it asked.
        raw_official = query.get("official")

        if raw_official is not None and type_name in ANON_OFFICIAL_TYPES:
            want = raw_official.strip().lower() not in ("0", "false", "no")
            rows = [r for r in rows if bool(r.get("isOfficial")) is want]

        page = max(1, int(query.get("page") or 1))
        limit = min(ANON_LIST_LIMIT, max(1, int(query.get("limit") or ANON_LIST_LIMIT)))
        total = len(rows)
        start = (page - 1) * limit

        self._json(
            200,
            {
                "data": [
                    _anon_summary(type_name, row) for row in rows[start : start + limit]
                ],
                "pagination": {
                    "page": page,
                    "limit": limit,
                    "total": total,
                    "totalPages": max(1, -(-total // limit)),
                },
                "note": ANON_LIST_NOTE,
            },
            {"x-cache": "MISS"},
        )

    # -- content -------------------------------------------------------------

    def _collection(self, method: str, type_name: str, query: dict[str, str]) -> None:
        if type_name not in KNOWN_TYPES:
            self._read_body()
            self._error(404, f"Unknown content type '{type_name}'.")
            return

        table = STATE.rows[type_name]

        if method == "GET":
            rows = sorted(table.values(), key=lambda r: r["id"], reverse=True)

            if query.get("mine") != "1":
                rows = [r for r in rows if not r.get("hidden")]

            if query.get("search"):
                needle = query["search"].lower()
                rows = [
                    r
                    for r in rows
                    if needle in str(r.get("name") or r.get("title") or "").lower()
                ]

            page = int(query.get("page") or 1)
            limit = min(int(query.get("limit") or 1000), 2500)
            total = len(rows)
            start = (page - 1) * limit

            self._json(
                200,
                {
                    "data": rows[start : start + limit],
                    "pagination": {
                        "page": page,
                        "limit": limit,
                        "total": total,
                        "totalPages": max(1, -(-total // limit)),
                    },
                },
            )
            return

        body = self._read_json()

        if method == "POST":
            bulk = isinstance(body, list)
            inputs = body if bulk else [body]

            if len(inputs) > 25:
                self._error(400, "At most 25 items may be created per request.")
                return

            for index, item in enumerate(inputs):
                missing = [f for f in REQUIRED.get(type_name, []) if f not in (item or {})]

                if missing:
                    self._error(
                        400,
                        "Validation failed.",
                        **({"index": index} if bulk else {}),
                        issues={"fieldErrors": {m: ["Required"] for m in missing}},
                    )
                    return

            created = []

            for item in inputs:
                row = {"id": STATE.take_id(), "hidden": False, **item}
                table[row["id"]] = row
                created.append(row)

            self._json(201, {"data": created if bulk else created[0]})
            return

        if method == "PUT":
            if not isinstance(body, list) or not body:
                self._error(400, "Bulk update expects a non-empty array of objects.")
                return

            if len(body) > 25:
                self._error(400, "At most 25 items may be updated per request.")
                return

            updated = []

            for item in body:
                row = table.get(item.get("id"))

                if row is None:
                    self._error(404, "Not found.")
                    return

                row.update({k: v for k, v in item.items() if k != "id"})
                updated.append(row)

            self._json(200, {"data": updated})
            return

        if method == "DELETE":
            ids = body if isinstance(body, list) else (body or {}).get("ids")

            if not isinstance(ids, list) or not ids or len(ids) > 100:
                self._error(400, "Bulk delete expects an array of ids.")
                return

            deleted = [i for i in ids if table.pop(i, None) is not None]

            self._json(200, {"data": {"deleted": deleted}})
            return

        self._error(405, "Method not allowed.")

    def _item(self, method: str, type_name: str, raw_id: str) -> None:
        if type_name not in KNOWN_TYPES:
            self._read_body()
            self._error(404, f"Unknown content type '{type_name}'.")
            return

        try:
            item_id = int(raw_id)
        except ValueError:
            self._read_body()
            self._error(400, "Invalid item id.")
            return

        table = STATE.rows[type_name]
        row = table.get(item_id)

        if method == "GET":
            if row is None:
                self._error(404, "Not found.")
                return

            self._json(200, {"data": row})
            return

        body = self._read_json()

        if row is None:
            self._error(404, "Not found.")
            return

        if method == "PUT":
            row.update(body or {})
            self._json(200, {"data": row})
            return

        if method == "DELETE":
            table.pop(item_id)
            self._json(200, {"data": {"id": item_id, "deleted": True}})
            return

        self._error(405, "Method not allowed.")

    # -- relations -----------------------------------------------------------

    def _relation(self, method: str, type_name: str, raw_id: str, relation: str) -> None:
        item_id = int(raw_id)
        key = (type_name, item_id, relation)

        if relation not in RELATION_PARENTS:
            self._read_body()
            self._error(404, f"Unknown relation '{relation}'.")
            return

        if type_name not in RELATION_PARENTS[relation]:
            self._read_body()
            self._error(404, f"{type_name} items have no '{relation}' relation.")
            return

        if STATE.rows.get(type_name, {}).get(item_id) is None:
            self._read_body()
            self._error(404, "Not found.")
            return

        existing = STATE.relations.setdefault(key, [])

        if method == "GET":
            self._json(200, {"data": existing})
            return

        body = self._read_json()

        if method == "DELETE":
            if body is None:
                STATE.relations[key] = []
            else:
                keys = body if isinstance(body, list) else body.get("ids") or body.get("tags")
                wanted = {k.lower() if isinstance(k, str) else k for k in keys}

                STATE.relations[key] = [
                    member
                    for member in existing
                    if _identity(relation, member) not in wanted
                ]

            self._json(200, {"data": STATE.relations[key]})
            return

        members = body.get("data") if isinstance(body, dict) else body

        if not isinstance(members, list):
            self._error(400, f"{method} expects a JSON array.")
            return

        if len(members) > 200:
            self._error(400, "Too many relation members.")
            return

        if method == "PUT":
            STATE.relations[key] = [_materialise(relation, m, None) for m in members]
        else:
            merged = list(existing)

            for member in members:
                identity = _identity(relation, member)
                at = next(
                    (
                        i
                        for i, e in enumerate(merged)
                        if identity is not None and _identity(relation, e) == identity
                    ),
                    -1,
                )

                if at >= 0:
                    merged[at] = _materialise(relation, member, merged[at])
                else:
                    merged.append(_materialise(relation, member, None))

            STATE.relations[key] = merged

        self._json(200, {"data": STATE.relations[key]})

    # -- files ---------------------------------------------------------------

    def _files(self, method: str, rest: list[str], query: dict[str, str]) -> None:
        if not rest:
            if method != "POST":
                self._read_body()
                self._error(405, "Method not allowed.")
                return

            content_type = self.headers.get("Content-Type") or ""
            raw = self._read_body()

            if content_type.lower().startswith("multipart/form-data"):
                parts, fields = _parse_multipart(raw, content_type)

                if len(parts) > 20:
                    self._error(400, "At most 20 files may be uploaded per request.")
                    return

                created = []

                for name, payload in parts:
                    created.append(self._store_file(name, payload, fields.get("title")))

                self._json(201, {"data": created})
                return

            name = self.headers.get("x-file-name") or query.get("name") or "file"
            row = self._store_file(name, raw, query.get("title"))

            self._json(201, {"data": row})
            return

        file_id = rest[0]
        row = STATE.files.get(file_id)

        if method == "GET":
            if row is None:
                self._error(404, "Not found.")
                return

            # A row may carry its own url, so a test can hand out a hostile one.
            self._json(200, {"data": {"url": f"https://cdn.test/{row['key']}", **row}})
            return

        body = self._read_json()

        if row is None:
            self._error(404, "Not found.")
            return

        if method in ("PUT", "PATCH"):
            row.update({k: v for k, v in (body or {}).items() if k in ("title", "description")})
            self._json(200, {"data": row})
            return

        if method == "DELETE":
            STATE.files.pop(file_id)
            self._json(200, {"data": {"id": file_id, "deleted": True}})
            return

        self._error(405, "Method not allowed.")

    def _store_file(self, name: str, payload: bytes, title: str | None) -> dict[str, Any]:
        if not payload:
            raise AssertionError("empty upload reached the mock")

        file_id = str(uuid.uuid4())
        row = {
            "id": file_id,
            "key": f"uploads/public/test/{file_id}/{name}",
            "type": "FILE_ZIP" if name.endswith(".zip") else "FILE",
            "size": len(payload),
            "title": title or name,
            "description": None,
            "isPublic": True,
        }

        STATE.files[file_id] = row

        return row


def _anon_summary(type_name: str, row: dict[str, Any]) -> dict[str, Any]:
    """The public summary — deliberately NOT the record.

    No owner, nothing from a relation table, and no `hidden`: the shape is the
    point, since a caller that got the record back would never notice that the
    real surface does not hand it over.
    """

    name = row.get("name") or row.get("title") or ""
    slug = row.get("url")

    return {
        "type": type_name,
        "id": row["id"],
        "name": name,
        "slug": slug,
        "path": f"/{type_name}/{slug or row['id']}",
        "url": None,
        "description": row.get("description"),
        "createdAt": row.get("createdAt"),
        "updatedAt": row.get("updatedAt"),
        # Present only when the model carries the column, as the real summary
        # does — `official` is absent, not false, on a type that has no such
        # flag.
        **({"official": bool(row["isOfficial"])} if "isOfficial" in row else {}),
    }


def _identity(relation: str, member: Any) -> Any:
    if relation == "tags":
        return member.strip().lower() if isinstance(member, str) else None

    if not isinstance(member, dict):
        return None

    if relation == "sources":
        return member.get("sourceId")

    return member.get("id")


def _materialise(relation: str, member: Any, previous: Any) -> Any:
    """Give a new member an id, mirroring what a create would do server-side."""

    if relation == "tags" or not isinstance(member, dict):
        return member

    row = dict(member)

    if "id" not in row:
        row["id"] = STATE.take_id()

    # The real release sync resets `hidden` when the field is omitted — modelled
    # here because the CLI's publish flow exists specifically to avoid it.
    if relation == "releases":
        row["hidden"] = bool(row.get("hidden", False))

        if "files" not in row and isinstance(previous, dict):
            row["files"] = previous.get("files", [])

    return row


def _parse_multipart(raw: bytes, content_type: str) -> tuple[list[tuple[str, bytes]], dict[str, str]]:
    match = re.search(r"boundary=([^;]+)", content_type)

    if not match:
        return [], {}

    boundary = match.group(1).strip('"').encode("ascii")
    chunks = raw.split(b"--" + boundary)

    files: list[tuple[str, bytes]] = []
    fields: dict[str, str] = {}

    for chunk in chunks:
        if chunk in (b"", b"--\r\n", b"--"):
            continue

        head, _, payload = chunk.partition(b"\r\n\r\n")

        if not payload:
            continue

        payload = payload.rstrip(b"\r\n")
        headers = head.decode("utf-8", "replace")

        filename = re.search(r'filename="([^"]*)"', headers)
        name = re.search(r'name="([^"]*)"', headers)

        if filename:
            files.append((filename.group(1), payload))
        elif name:
            fields[name.group(1)] = payload.decode("utf-8", "replace")

    return files, fields


class MockServer:
    """Context manager wrapping the handler in a background HTTP server."""

    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]

        return f"http://{host}:{port}"

    def __enter__(self) -> "MockServer":
        STATE.reset()
        self.thread.start()

        return self

    def __exit__(self, *exc: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


# ---- the app API as a signed-in member ----------------------------------------
#
# The device flow, token rotation and the per-user routes, modelled on website-
# city `src/app/api/app/v1/**` and `src/lib/app-api/handler.ts`:
#
#   - errors are the FLAT envelope `{ok:false, code, message}`;
#   - the poll answers 202 for `authorization_pending` / `slow_down` and 400 for
#     `access_denied` / `expired_token`, and checks PKCE (S256) like `VerifyPkce`;
#   - a presented-but-dead access token is `401 token_expired`, no header at all
#     is `401 unauthorized` — the distinction the CLI's refresh keys on;
#   - `auth/refresh` ROTATES: the old refresh token is retired, and presenting a
#     retired one revokes the family (`token_reuse`), as `RotateAppToken` does.

APP_USER_ROOTS = {"auth", "me", "friends", "party", "presence", "installs", "subscriptions", "stats", "downloads", "avatar", "report"}

APP_USERS = {
    "u1": {"id": "u1", "name": "Ann", "username": "ann", "avatar": None, "role": "USER"},
    "u2": {"id": "u2", "name": "Bob", "username": "bob", "avatar": None, "role": "USER"},
    "u3": {"id": "u3", "name": "Cat", "username": "cat", "avatar": None, "role": "USER"},
}

SETTINGS_DEFAULT: dict[str, Any] = {
    "timezone": "UTC", "locale": "en",
    **{k: True for k in ("emailNotifications", "pushNotifications", "notifySystem", "notifyComments", "notifyReviews",
                         "notifyReleases", "notifyMentions", "notifyModeration", "notifyCredits", "notifyFriends",
                         "notifyFollowers", "notifyMessages", "notifyStatus", "notifyParties", "notifyGroups")},
}

OFFLINE = {"status": "offline", "appId": None, "appName": None, "serverId": None, "serverName": None,
           "partyId": None, "joinable": False, "detail": None, "updatedAt": None}


class AppUserState:
    def __init__(self) -> None:
        #: deviceCode → grant. `plan` is what successive polls answer before the
        #: grant is approved: e.g. ["authorization_pending", "slow_down"].
        self.grants: dict[str, dict[str, Any]] = {}
        self.plan: list[str] = ["authorization_pending"]
        self.polls: list[str] = []
        self.access: dict[str, str] = {}
        self.refresh: dict[str, str] = {}
        self.retired: set[str] = set()
        self.revoked = 0
        self.refreshes = 0
        self.serial = 0
        self.friends = {"u1": ["u2"]}
        self.requests = [{"id": 11, "from": "u3", "to": "u1", "createdAt": "2026-10-01T10:00:00Z"}]
        self.presence = {"u2": {**OFFLINE, "status": "in_game", "appId": 1, "appName": "Seed", "updatedAt": "2026-10-07T10:00:00Z"}}
        self.parties: dict[str, dict[str, Any]] = {}
        self.invites: list[dict[str, Any]] = [{"id": "900", "partyId": "500", "partyName": "Bob's", "inviterId": "u2", "message": None, "to": "u1"}]
        self.installs: dict[int, dict[str, Any]] = {}
        self.subs: dict[tuple[str, int], dict[str, Any]] = {}
        self.reviews: dict[tuple[str, int], dict[str, Any]] = {}
        self.stats = {1: {"kills": [("u2", 50.0), ("u1", 30.0), ("u3", 10.0)]}}
        #: `UserSettingsSchema`, per user; PATCH merges a partial into it.
        self.settings: dict[str, dict[str, Any]] = {}
        self.downloads = {"u1": [
            {"deviceName": "Desktop", "tokenId": "other", "active": 1, "queued": 2, "paused": 0, "failed": 0, "finished": 3,
             "speedBps": 1048576, "remainingBytes": 5000000000, "items": [{"name": "Big Pack", "progress": 0.4}],
             "updatedAt": "2026-10-13T08:00:00Z"},
        ]}
        self.avatars: dict[str, dict[str, Any]] = {}
        self.reports: list[dict[str, Any]] = []
        self.reservations: dict[str, dict[str, Any]] = {}

    def next(self) -> int:
        self.serial += 1
        return self.serial

    def issue(self, user_id: str) -> dict[str, Any]:
        n = self.next()
        access, refresh = f"tmc_at_{n:04d}" + "x" * 40, f"tmc_rt_{n:04d}" + "y" * 40
        self.access[access] = user_id
        self.refresh[refresh] = user_id
        return {"accessToken": access, "refreshToken": refresh, "expiresIn": 3600, "user": APP_USERS[user_id]}

    def expire_all_access(self) -> None:
        """Every access token issued so far now reads as dead (`token_expired`)."""
        self.access.clear()


def _b64u_sha256(text: str) -> str:
    import base64
    import hashlib

    return base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=").decode()


def _app_user(h: "Handler", method: str, route: str, query: dict[str, list[str]], body: Any) -> None:
    app = STATE.app

    def ok(data: Any, status: int = 200) -> None:
        h._json(status, {"ok": True, "data": data})

    def fail(status: int, code: str, message: str) -> None:
        h._json(status, {"ok": False, "code": code, "message": message})

    def q(key: str) -> str | None:
        return query.get(key, [None])[-1]

    b = body if isinstance(body, dict) else {}

    # ---- auth: none
    if route == "auth/device" and method == "POST":
        client = b.get("client") or {}
        challenge = b.get("codeChallenge")
        if not (isinstance(challenge, str) and 43 <= len(challenge) <= 128) or client.get("platform") not in (
            "windows", "macos", "linux", "android", "ios", "unknown"
        ) or not client.get("name") or not client.get("version"):
            return fail(400, "bad_request", "Invalid request.")
        code = f"tmc_dc_{app.next():04d}" + "z" * 30
        app.grants[code] = {"challenge": challenge, "plan": list(app.plan), "client": client}
        return ok({"deviceCode": code, "userCode": "WDJB-MJHT", "verificationUri": "https://example.test/login/device",
                   "verificationUriComplete": "https://example.test/login/device?code=WDJB-MJHT", "expiresIn": 600, "interval": 5})

    if route == "auth/token" and method == "POST":
        grant = app.grants.get(b.get("deviceCode") or "")
        verifier = b.get("codeVerifier") or ""
        if grant is None:
            return fail(400, "expired_token", "This login attempt expired. Start again.")
        if not (43 <= len(verifier) <= 128) or _b64u_sha256(verifier) != grant["challenge"]:
            app.grants.pop(b["deviceCode"], None)
            return fail(400, "expired_token", "This login attempt expired. Start again.")
        if grant["plan"]:
            outcome = grant["plan"].pop(0)
            app.polls.append(outcome)
            if outcome in ("authorization_pending", "slow_down"):
                return fail(202, outcome, "Waiting." if outcome == "authorization_pending" else "Polling too quickly.")
            app.grants.pop(b["deviceCode"], None)
            return fail(400, outcome, "This device was not approved." if outcome == "access_denied" else "Expired.")
        app.polls.append("approved")
        app.grants.pop(b["deviceCode"], None)
        return ok(app.issue("u1"))

    if route == "auth/refresh" and method == "POST":
        token = b.get("refreshToken") or ""
        app.refreshes += 1
        if token in app.retired:
            app.access.clear()
            app.refresh.clear()
            return fail(401, "token_reuse", "This session was signed out because its credentials were used from somewhere else.")
        user = app.refresh.pop(token, None)
        if user is None:
            return fail(401, "invalid_grant", "Sign in again.")
        app.retired.add(token)
        return ok(app.issue(user))

    # ---- auth: required
    header = h.headers.get("Authorization") or ""
    bearer = header[7:].strip() if header.startswith("Bearer ") else ""
    if bearer and bearer not in app.access:
        return fail(401, "token_expired", "This session has expired. Sign in again.")
    if not bearer:
        return fail(401, "unauthorized", "Authentication is required.")
    me = app.access[bearer]

    def bad(message: str = "Invalid request.") -> None:
        fail(400, "bad_request", message)

    def digits(v: Any) -> bool:
        return isinstance(v, str) and v.isdigit() and len(v) <= 20

    if route == "auth/revoke" and method == "POST":
        app.access = {k: v for k, v in app.access.items() if v != me}
        app.refresh = {k: v for k, v in app.refresh.items() if v != me}
        app.revoked += 1
        return ok({"revoked": True})

    def settings() -> dict[str, Any]:
        return {**SETTINGS_DEFAULT, **app.settings.get(me, {})}

    if route == "me" and method == "GET":
        return ok({"user": APP_USERS[me], "settings": settings(), "serverTime": int(time.time())})
    if route == "me" and method == "PATCH":
        # `UserSettingsPatchSchema` is a zod object: an unknown key is stripped,
        # a wrong type is a 400.
        for k, v in b.items():
            if k in SETTINGS_DEFAULT and type(v) is not type(SETTINGS_DEFAULT[k]):
                return bad(f"Expected {type(SETTINGS_DEFAULT[k]).__name__} for {k}")
        app.settings.setdefault(me, {}).update({k: v for k, v in b.items() if k in SETTINGS_DEFAULT})
        return ok(settings())

    # downloads: GET only from a terminal; POST/DELETE are the app's reports.
    if route == "downloads" and method == "GET":
        return ok({"devices": [{**{k: v for k, v in d.items() if k != "tokenId"}, "isThisDevice": d["tokenId"] == bearer}
                               for d in app.downloads.get(me, [])]})

    # avatar: the caller's own
    if route == "avatar" and method == "GET":
        return ok(app.avatars.get(me, {"version": 1, "schema_id": "backbone.v1", "parts": {}, "colours": {}}))
    if route == "avatar" and method == "PUT":
        if b.get("schema_id", "backbone.v1") != "backbone.v1":
            return fail(409, "schema_mismatch", "That document is for another avatar schema.")
        app.avatars[me] = {"version": 1, "schema_id": "backbone.v1", **b}
        return ok(app.avatars[me])

    # report: one open report per (member, item); a second answers success.
    if route == "report" and method == "POST":
        if b.get("kind") not in ("asset", "mod", "server", "serverMap", "article", "community", "collection", "user") \
                or not isinstance(b.get("id"), int) or b.get("type", "OTHER") not in ("SPAM", "OTHER") \
                or not 3 <= len(b.get("title") or "") <= 120 or not 10 <= len(b.get("content") or "") <= 4000:
            return bad()
        if b["kind"] == "collection":
            return fail(400, "unsupported_kind", "That kind of item cannot be reported here.")
        if b["id"] >= 1000:
            return fail(404, "not_found", "That item does not exist.")
        if any(r["kind"] == b["kind"] and r["id"] == b["id"] and r["owner"] == me for r in app.reports):
            return ok({"reported": True})
        app.reports.append({**b, "owner": me})
        return ok({"reported": True}, 201)

    # friends
    def display(uid: str) -> str:
        return APP_USERS[uid]["name"]

    if route == "friends" and method == "GET":
        return ok([{"userId": f, "displayName": display(f), "avatarUrl": None, "presence": app.presence.get(f, OFFLINE)}
                   for f in app.friends.get(me, [])])
    if route == "friends/requests" and method == "GET":
        def row(r: dict[str, Any], other: str) -> dict[str, Any]:
            return {"id": r["id"], "userId": other, "displayName": display(other), "createdAt": r["createdAt"]}
        return ok({"incoming": [row(r, r["from"]) for r in app.requests if r["to"] == me],
                   "outgoing": [row(r, r["to"]) for r in app.requests if r["from"] == me]})
    if route == "friends/request" and method == "POST":
        uid = b.get("userId")
        if not isinstance(uid, str) or not uid:
            return bad()
        if uid not in APP_USERS or uid == me:
            return fail(404, "friends.request.deny.notFound", "That member could not be found.")
        rid = 100 + app.next()
        app.requests.append({"id": rid, "from": me, "to": uid, "createdAt": "2026-10-07T00:00:00Z"})
        return ok({"id": rid})
    if route in ("friends/respond", "friends/cancel") and method == "POST":
        rid = b.get("requestId")
        if not isinstance(rid, int) or (route == "friends/respond" and not isinstance(b.get("accept"), bool)):
            return bad()
        mine = "to" if route == "friends/respond" else "from"
        match = next((r for r in app.requests if r["id"] == rid and r[mine] == me), None)
        if match is None:
            return fail(404, f"{route.replace('/', '.')}.deny.notFound", "That request could not be found.")
        app.requests.remove(match)
        if route == "friends/respond" and b["accept"]:
            app.friends.setdefault(me, []).append(match["from"])
        return ok(None)
    if route == "friends/remove" and method == "POST":
        uid = b.get("userId")
        if uid not in app.friends.get(me, []):
            return fail(404, "friends.remove.deny.notFound", "You are not friends.")
        app.friends[me].remove(uid)
        return ok(None)

    # presence
    if route == "presence" and method == "GET":
        raw = q("userIds")
        if len(query.get("userIds", [])) > 1:
            return bad("Expected string, received array")
        ids = [i for i in (raw or "").split(",") if i]
        return ok({i: app.presence.get(i, OFFLINE) for i in ids if i in app.friends.get(me, [])})
    if route == "presence" and method == "POST":
        status = b.get("status")
        if status not in ("offline", "online", "in_game", "away"):
            return fail(400, "presence.deny.status", "A presence is offline, online, in_game or away.")
        if b.get("partyId") is not None and not digits(b["partyId"]):
            return bad()
        app.presence[me] = {**OFFLINE, "status": status, "serverId": b.get("serverId"), "partyId": b.get("partyId"),
                            "joinable": bool(b.get("joinable")), "detail": b.get("detail")}
        return ok({"ttlSec": 120})

    # parties
    def party_view(pid: str) -> dict[str, Any]:
        p = app.parties[pid]
        members = [{"userId": u, "displayName": display(u), "gameName": None, "role": "HOST" if u == p["hostId"] else "MEMBER",
                    "state": "JOINED", "presence": "ONLINE", "joinedAt": "2026-10-07T00:00:00Z", "leftAt": None,
                    "readyAt": "2026-10-07T00:01:00Z" if u in p["ready"] else None, "connectedAt": None,
                    "score": 0, "kills": 0, "deaths": 0, "assist": 0} for u in p["members"]]
        return {**{k: v for k, v in p.items() if k not in ("members", "ready", "password")}, "users": len(members), "members": members}

    def ready_view(pid: str) -> dict[str, Any]:
        p = app.parties[pid]
        total, ready = len(p["members"]), len(p["ready"])
        return {"stage": p["stage"], "openedAt": None, "elapsedSec": 0, "total": total, "ready": ready,
                "pct": ready * 100 // total if total else 0, "autoPct": 0, "autoSec": 30, "countdownSec": None,
                "members": [], "membersHidden": False,
                "me": {"isMember": me in p["members"], "ready": me in p["ready"], "connected": False, "canManage": p["hostId"] == me},
                "connect": None, "started": p["stage"] == "PLAYING"}

    def mine() -> str | None:
        return next((pid for pid, p in app.parties.items() if me in p["members"]), None)

    def known(pid: Any) -> bool:
        return digits(pid) and pid in app.parties

    if route == "party/mine" and method == "GET":
        pid = mine()
        return ok(party_view(pid) if pid else None)
    if route.startswith("party/") and route.count("/") == 1 and method == "GET" and route[6:].isdigit():
        pid = route[6:]
        return ok(party_view(pid)) if pid in app.parties else fail(404, "not_found", "Not found.")
    if route == "party/create" and method == "POST":
        if not isinstance(b.get("appId"), int):
            return bad("Expected number, received string")
        if b.get("type", "PUBLIC") not in ("PUBLIC", "PUBLIC_PASSWORD", "FRIENDS", "PRIVATE"):
            return bad()
        pid = str(500 + app.next())
        app.parties[pid] = {"id": pid, "name": b.get("name"), "type": b.get("type", "PUBLIC"), "techType": "WEB",
                            "maxUsers": b.get("maxUsers", 8), "stage": "LOBBY", "startTime": "2026-10-07T00:00:00Z",
                            "endTime": None, "mapName": None, "gameMode": None, "hostId": me, "appId": b["appId"],
                            "serverId": b.get("serverId"), "members": [me], "ready": set(), "password": b.get("password")}
        return ok(party_view(pid))
    if route == "party/invite" and method == "POST":
        if not known(b.get("id")) or not isinstance(b.get("userIds"), list) or not b["userIds"]:
            return bad() if not digits(b.get("id")) else fail(404, "not_found", "Not found.")
        ids = []
        for uid in b["userIds"]:
            iid = str(900 + app.next())
            app.invites.append({"id": iid, "partyId": b["id"], "partyName": app.parties[b["id"]]["name"], "inviterId": me, "message": b.get("message"), "to": uid})
            ids.append(iid)
        return ok(ids)
    if route == "party/invites" and method == "GET":
        return ok([{k: v for k, v in i.items() if k != "to"} for i in app.invites if i["to"] == me])
    if route == "party/invite/respond" and method == "POST":
        if not digits(b.get("inviteId")) or not isinstance(b.get("accept"), bool):
            return bad()
        inv = next((i for i in app.invites if i["id"] == b["inviteId"] and i["to"] == me), None)
        if inv is None:
            return fail(404, "party.invite.deny.notFound", "That invite could not be found.")
        app.invites.remove(inv)
        if not b["accept"]:
            return ok(None)
        app.parties.setdefault(inv["partyId"], {"id": inv["partyId"], "name": inv["partyName"], "type": "PUBLIC", "techType": "WEB",
                                                "maxUsers": 8, "stage": "LOBBY", "startTime": "2026-10-07T00:00:00Z", "endTime": None,
                                                "mapName": None, "gameMode": None, "hostId": "u2", "appId": 1, "serverId": None,
                                                "members": ["u2"], "ready": set(), "password": None})["members"].append(me)
        return ok({"partyId": inv["partyId"], "role": "MEMBER", "leftPartyId": None})
    if route == "party/join" and method == "POST":
        if not digits(b.get("id")):
            return bad()
        if b["id"] not in app.parties:
            return fail(404, "party.join.deny.notFound", "That party could not be found.")
        p = app.parties[b["id"]]
        if p["type"] == "PUBLIC_PASSWORD" and b.get("password") != p["password"]:
            return fail(403, "party.join.deny.password", "Wrong password.")
        left = mine()
        if left:
            app.parties[left]["members"].remove(me)
        p["members"].append(me)
        return ok({"partyId": b["id"], "role": "MEMBER", "leftPartyId": left})
    if route == "party/leave" and method == "POST":
        if not known(b.get("id")) or me not in app.parties[b["id"]]["members"]:
            return fail(404, "not_found", "Not found.")
        p = app.parties[b["id"]]
        p["members"].remove(me)
        return ok({"left": True, "ended": not p["members"]})
    if route == "party/kick" and method == "POST":
        if not known(b.get("id")) or not isinstance(b.get("userId"), str):
            return bad()
        p = app.parties[b["id"]]
        if p["hostId"] != me:
            return fail(403, "party.kick.deny.notManager", "Only the host or a co-host can do that.")
        if b["userId"] not in p["members"]:
            return fail(404, "party.kick.deny.notMember", "They are not in this party.")
        p["members"].remove(b["userId"])
        return ok(None)
    if route == "party/ready" and method == "POST":
        if not known(b.get("id")):
            return fail(404, "not_found", "Not found.")
        p = app.parties[b["id"]]
        (p["ready"].add if b.get("ready", True) else p["ready"].discard)(me)
        return ok(ready_view(b["id"]))
    if route == "party/start" and method == "POST":
        if not known(b.get("id")):
            return fail(404, "not_found", "Not found.")
        p = app.parties[b["id"]]
        if p["hostId"] != me:
            return fail(403, "party.start.deny.notManager", "Only the host or a co-host can do that.")
        if b.get("serverId") is not None:
            p["serverId"] = b["serverId"]
        if p["serverId"] is None:
            return fail(400, "party.start.deny.noServer", "Pick a server first.")
        p["stage"] = "PLAYING"
        return ok(ready_view(b["id"]))

    if route.startswith("party/") and route.endswith("/ready") and method == "GET":
        pid = route.split("/")[1]
        if pid not in app.parties or me not in app.parties[pid]["members"]:
            return fail(404, "not_found", "Not found.")
        return ok(ready_view(pid))

    def manager(pid: Any, action: str) -> str | None:
        """None when allowed, else a refusal already sent."""
        if not digits(pid):
            bad()
            return "sent"
        if pid not in app.parties:
            fail(404, "not_found", "Not found.")
            return "sent"
        p = app.parties[pid]
        if p["hostId"] != me and me not in p.get("cohosts", set()):
            fail(403, f"party.{action}.deny.notManager", "Only the host or a co-host can do that.")
            return "sent"
        return None

    if route in ("party/force-start", "party/cancel", "party/end") and method == "POST":
        action = route[6:]
        if manager(b.get("id"), action):
            return None
        p = app.parties[b["id"]]
        if route == "party/force-start":
            if p["serverId"] is None:
                return fail(400, "party.forceStart.deny.noServer", "Pick a server first.")
            p["stage"] = "PLAYING"
        elif route == "party/cancel":
            if p["stage"] != "READY":
                return fail(409, "party.ready.deny.notReady", "There is no ready round to cancel.")
            p["stage"] = "LOBBY"
            p["ready"] = set()
        else:
            p["stage"] = "ENDED"
        return ok(None)
    if route == "party/role" and method == "POST":
        if b.get("role") not in ("CO_HOST", "MEMBER") or not isinstance(b.get("userId"), str):
            return bad()
        if manager(b.get("id"), "role"):
            return None
        p = app.parties[b["id"]]
        if b["userId"] not in p["members"] or b["userId"] == p["hostId"]:
            return fail(404, "party.role.deny.notMember", "They are not a member of this party.")
        cohosts = p.setdefault("cohosts", set())
        (cohosts.add if b["role"] == "CO_HOST" else cohosts.discard)(b["userId"])
        return ok(None)
    if route == "party/transfer" and method == "POST":
        if not known(b.get("id")) or not isinstance(b.get("userId"), str):
            return bad()
        p = app.parties[b["id"]]
        if p["hostId"] != me:
            return fail(403, "party.transfer.deny.notHost", "Only the host can hand the party over.")
        if b["userId"] not in p["members"]:
            return fail(404, "party.transfer.deny.notMember", "They are not in this party.")
        p["hostId"] = b["userId"]
        return ok(None)
    if route.startswith("party/") and route.endswith("/reserve/offer") and method == "GET":
        pid, server = route.split("/")[1], q("serverId")
        if not (server or "").isdigit():
            return bad("Expected number, received nan")
        if pid not in app.parties or me not in app.parties[pid]["members"]:
            return fail(404, "not_found", "Not found.")
        return ok({"available": True, "maxMinutes": 120, "privateOffered": False, "approval": "AUTO", "cooldownSec": 0})
    if route == "party/reserve" and method == "POST":
        minutes = b.get("minutes")
        if not digits(b.get("partyId")) or not isinstance(b.get("serverId"), int) or not isinstance(minutes, int) \
                or not 5 <= minutes <= 1440 or not isinstance(b.get("private", False), bool):
            return bad()
        if manager(b["partyId"], "reserve"):
            return None
        if b.get("private"):
            return fail(400, "party.reserve.deny.private", "This server does not offer private lobbies.")
        rid = str(700 + app.next())
        app.reservations[rid] = {"id": rid, "status": "PENDING" if b["serverId"] == 8 else "APPROVED",
                                 "private": False, "startsAt": "2026-10-13T09:00:00Z",
                                 "endsAt": f"+{min(minutes, 120)}m", "releasedAt": None, "note": b.get("note"),
                                 "partyId": b["partyId"], "partyName": app.parties[b["partyId"]]["name"], "partyUsers": 1,
                                 "server": {"id": b["serverId"], "name": f"Server {b['serverId']}", "url": "s", "online": True}}
        return ok(app.reservations[rid])
    if route == "party/reserve/cancel" and method == "POST":
        rid = b.get("reservationId")
        if not digits(rid):
            return bad()
        row = app.reservations.get(rid)
        if row is None:
            return fail(404, "not_found", "Not found.")
        if manager(row["partyId"], "reserve"):
            return None
        row["status"] = "CANCELLED"
        return ok(row)

    # installs
    def install_view(iid: int) -> dict[str, Any]:
        i = app.installs[iid]
        return {**{k: v for k, v in i.items() if k != "owner"}, "items": sorted(i["items"], key=lambda x: x["order"])}

    if route == "installs" and method == "GET":
        app_id = q("appId")
        return ok({"installs": [install_view(k) for k, i in app.installs.items()
                                if i["owner"] == me and (app_id is None or str(i["appId"]) == app_id)]})
    if route == "installs" and method == "POST":
        if not isinstance(b.get("appId"), int) or not isinstance(b.get("name"), str) or not b["name"]:
            return bad()
        if b["appId"] != 1:
            return fail(400, "app_unsupported", "The official app cannot manage installs for this game yet.")
        iid = app.next()
        first = not any(i["owner"] == me and i["appId"] == b["appId"] for i in app.installs.values())
        app.installs[iid] = {"id": iid, "owner": me, "appId": b["appId"], "app": {"id": 1, "name": "Seed", "slug": "seed", "icon": None},
                             "name": b["name"], "description": b.get("description"), "gameVersion": b.get("gameVersion"),
                             "loader": b.get("loader"), "isDefault": b.get("isDefault", first),
                             "strategy": b.get("strategy", "hardlink"), "environment": b.get("environment", "client"),
                             "launchArgs": [], "launchEnv": {}, "options": {}, "createdAt": "2026-10-07", "updatedAt": "2026-10-07",
                             "lastPlayedAt": None, "playSeconds": 0, "items": []}
        return ok(install_view(iid), 201)
    if route in ("installs", "installs/items") and method in ("PATCH", "DELETE", "POST"):
        key = "id" if route == "installs" else "installId"
        iid = b.get(key)
        if not isinstance(iid, int):
            return bad()
        if iid not in app.installs or app.installs[iid]["owner"] != me:
            return fail(404, "not_found", "That install does not exist.")
        inst = app.installs[iid]
        if route == "installs" and method == "DELETE":
            del app.installs[iid]
            return ok({"deleted": True})
        if route == "installs":
            for k in ("name", "description", "gameVersion", "loader", "strategy", "environment"):
                if k in b:
                    inst[k] = b[k]
            if b.get("isDefault") is True:
                inst["isDefault"] = True
            return ok(install_view(iid))
        if b.get("kind") not in ("asset", "mod", "collection") or not isinstance(b.get("itemId"), int):
            return bad()
        match = next((x for x in inst["items"] if x["kind"] == b["kind"] and x["itemId"] == b["itemId"]), None)
        if method == "POST" and match is None:
            inst["items"].append({"id": app.next(), "kind": b["kind"], "itemId": b["itemId"], "name": f"{b['kind']} {b['itemId']}",
                                  "webUrl": "https://x", "image": None, "enabled": True, "order": len(inst["items"]),
                                  "subscribed": (b["kind"], b["itemId"]) in app.subs})
        elif method == "PATCH" and match is not None:
            for k in ("enabled", "order"):
                if k in b:
                    match[k] = b[k]
        elif method == "DELETE" and match is not None:
            inst["items"].remove(match)
        return ok(install_view(iid))

    # subscriptions
    if route == "subscriptions" and method == "GET":
        items = [{"id": s["id"], "kind": k[0], "itemId": k[1], "name": f"{k[0]} {k[1]}", "description": None, "image": None,
                  "webUrl": "https://x", "app": {"id": 1, "name": "Seed", "slug": "seed"}, "createdAt": "2026-10-07",
                  "updatedAt": "2026-10-07", "autoUpdate": s["autoUpdate"], "notifyUpdates": s["notifyUpdates"],
                  "paused": s["paused"], "viaCollectionId": None, "installable": True,
                  "release": {"id": 9, "version": "1.0", "createdAt": "2026-10-07", "file": None}}
                 for k, s in app.subs.items()]
        return ok({"items": items, "removed": [], "revision": str(app.serial), "full": q("since") is None})
    if route == "subscriptions" and method == "POST":
        if b.get("kind") not in ("asset", "mod", "collection") or not isinstance(b.get("itemId"), int):
            return bad()
        key = (b["kind"], b["itemId"])
        if b["itemId"] >= 1000:
            return fail(404, "not_found", "That item is not available.")
        want = b.get("subscribed", key not in app.subs)
        if want:
            app.subs.setdefault(key, {"id": f"s{app.next()}", "autoUpdate": True, "notifyUpdates": True, "paused": False})
        else:
            app.subs.pop(key, None)
        return ok({"subscribed": want, "propagated": 0, "skipped": 0})
    if route == "subscriptions/prefs" and method == "POST":
        key = (b.get("kind"), b.get("itemId"))
        if key not in app.subs:
            return fail(404, "not_found", "You are not subscribed to that item.")
        for k in ("autoUpdate", "notifyUpdates", "paused"):
            if isinstance(b.get(k), bool):
                app.subs[key][k] = b[k]
        return ok({"ok": True})

    # reviews/write
    if route == "reviews/write" and method == "POST":
        rating, content = b.get("rating"), b.get("content")
        if not isinstance(b.get("id"), int) or (rating is not None and not (isinstance(rating, int) and 1 <= rating <= 5)):
            return bad()
        if rating is None and not (content or "").strip():
            return bad("Give it a score, some words, or both.")
        key = (b.get("kind"), b["id"])
        edited = key in app.reviews
        app.reviews[key] = {"id": app.reviews.get(key, {}).get("id") or app.next(), "rating": rating, "content": content, "owner": me}
        return ok({"saved": True, "edited": edited, "held": False})
    if route == "reviews/write" and method == "DELETE":
        match = next((k for k, r in app.reviews.items() if r["id"] == b.get("id") and r["owner"] == me), None)
        if match is None:
            return fail(404, "not_found", "That review does not exist.")
        del app.reviews[match]
        return ok({"deleted": True})
    if route == "reviews/write" and method == "PUT":
        if not isinstance(b.get("id"), int) or "positive" not in b or b["positive"] not in (True, False, None):
            return bad()
        if any(r["id"] == b["id"] and r["owner"] == me for r in app.reviews.values()):
            return fail(400, "own_review", "You cannot vote on your own review.")
        return ok({"voted": b["positive"]})

    # stats
    if route in ("stats/top", "stats/me", "stats/defs") and method == "GET":
        if not (q("app") or "").isdigit():
            return fail(400, "invalid", "Say which app to read.")
        game = app.stats.get(int(q("app") or 0))
        if game is None:
            return fail(404, "not_found", "No such app.")
        if route == "stats/defs":
            return ok([{"key": k, "name": k.title(), "description": None, "kind": "COUNTER", "unit": "", "decimals": 0, "players": len(v)}
                       for k, v in sorted(game.items())])
        if route == "stats/me":
            return ok({"player": f"p-{me}", "name": APP_USERS[me]["name"],
                       "stats": [{"key": k, "name": k.title(), "kind": "COUNTER", "unit": "", "decimals": 0, "value": dict(v)[me]}
                                 for k, v in game.items() if me in dict(v)]})
        stat = q("stat")
        if stat not in game:
            return fail(404, "not_found", "No such stat.")
        offset, limit = int(q("offset") or 0), int(q("limit") or 25)
        ranked = [{"rank": n + 1, "player": f"p-{u}", "name": APP_USERS[u]["name"], "value": v} for n, (u, v) in enumerate(game[stat])]
        return ok({"stat": {"key": stat, "name": stat.title(), "kind": "COUNTER", "unit": "", "decimals": 0, "players": len(ranked)},
                   "rows": ranked[offset:offset + limit], "self": next((r for r in ranked if r["player"] == f"p-{me}"), None)})

    fail(404, "not_found", "Not found.")


#: Built last: `State.reset` needs `AppUserState`, which is defined below `State`.
STATE = State()
