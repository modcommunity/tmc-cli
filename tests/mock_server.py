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

    def take_id(self) -> int:
        value = self.next_id
        self.next_id += 1

        return value


STATE = State()


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
