"""The content API, as methods.

Everything the commands do goes through here, and this is where the API's own
limits stop being the caller's problem: bulk writes are capped at 25 per
request, deletes at 100, relation members at 200, uploads at 20 parts. A caller
with 400 things to create should say so and get 400 things created, so these
methods batch and re-batch rather than making the user do arithmetic.

The one thing NOT hidden is that bulk writes are not transactional. When a batch
fails partway the server reports what did land, and `BulkResult` carries that
through to the surface instead of averaging it into "failed" — knowing 73 of 400
items exist is the difference between resuming and starting over.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from . import output
from .errors import ApiError, UsageError
from .http import FilePart, Response, Transport, chunked
from .schema import (
    ANON_APP_FILTER_TYPES,
    ANON_LIST_FILTERS,
    ANON_TYPES,
    MAX_BULK_DELETE,
    MAX_BULK_WRITE,
    MAX_RELATION_KEYS,
    MAX_RELATION_MEMBERS,
    MAX_UPLOAD_PARTS,
    RELATIONS,
    TYPES,
    relations_for,
)

Progress = Callable[[str], None]


@dataclass
class BulkResult:
    """The outcome of a batched write, including a partial one."""

    items: list[Any] = field(default_factory=list)
    #: Set when a batch failed; the items above are what landed before it.
    error: ApiError | None = None
    #: Index of the failing element, counted across the whole input.
    failed_index: int | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def raise_if_failed(self) -> "BulkResult":
        if self.error is not None:
            raise self.error

        return self


class ContentClient:
    def __init__(self, transport: Transport, progress: Progress | None = None) -> None:
        self.http = transport
        self._progress = progress or (lambda _message: None)

    # -- content items -------------------------------------------------------

    def list(
        self,
        type_name: str,
        *,
        page: int = 1,
        limit: int | None = None,
        mine: bool = False,
        filters: dict[str, Any] | None = None,
        all_pages: bool = False,
        max_items: int | None = None,
    ) -> tuple[list[Any], dict[str, Any] | None]:
        """List rows. With `all_pages`, walks the pagination itself.

        `?mine=1` is the only way to see your own hidden rows — the list endpoint
        drops hidden rows for everyone else, and a plain owner filter is not
        enough to unlock them server-side (it checks that the query is actually
        pinned to you).
        """

        params: dict[str, Any] = dict(filters or {})

        if self.http.anonymous:
            params = self._anon_list_params(type_name, params, mine)

        params["page"] = page

        if limit is not None:
            params["limit"] = limit

        if mine and not self.http.anonymous:
            params["mine"] = 1

        response = self.http.request("GET", self._path(type_name), params=params)

        # The anonymous listing states its own limits in a `note` rather than
        # leaving a caller to wonder why every row has no `stats`. Passed
        # through verbatim: it is the server's sentence about the server's
        # behaviour, and paraphrasing it here is how a mirror goes stale.
        if isinstance(response.body, dict) and response.body.get("note"):
            output.info(f"  {response.body['note']}")

        rows = response.data if isinstance(response.data, list) else []
        pagination = response.pagination

        if not all_pages or not pagination:
            return rows, pagination

        total_pages = int(pagination.get("totalPages") or 1)
        current = int(pagination.get("page") or page)

        while current < total_pages:
            if max_items is not None and len(rows) >= max_items:
                break

            current += 1
            params["page"] = current

            self._progress(f"fetching page {current}/{total_pages}")

            more = self.http.request("GET", self._path(type_name), params=params)

            if isinstance(more.data, list):
                rows.extend(more.data)

            if more.pagination:
                total_pages = int(more.pagination.get("totalPages") or total_pages)

        if max_items is not None:
            rows = rows[:max_items]

        return rows, pagination

    def get(self, type_name: str, item_id: int) -> Any:
        self._check_anonymous_type(type_name)

        return self.http.request("GET", self._path(type_name, item_id)).data

    def create(self, type_name: str, payload: dict[str, Any]) -> Any:
        return self.http.request("POST", self._path(type_name), json_body=payload).data

    def create_many(self, type_name: str, payloads: Sequence[dict[str, Any]]) -> BulkResult:
        """Create any number of items, in batches of 25."""

        return self._batched_write(
            "POST", type_name, payloads, MAX_BULK_WRITE, "created"
        )

    def update(self, type_name: str, item_id: int, payload: dict[str, Any]) -> Any:
        return self.http.request(
            "PUT", self._path(type_name, item_id), json_body=payload
        ).data

    def update_many(self, type_name: str, payloads: Sequence[dict[str, Any]]) -> BulkResult:
        """Bulk update, in batches of 25. Every element needs an `id`."""

        for index, item in enumerate(payloads):
            if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                raise UsageError(
                    f"Element {index} of a bulk update needs an integer 'id'."
                )

        return self._batched_write("PUT", type_name, payloads, MAX_BULK_WRITE, "updated")

    def delete(self, type_name: str, item_id: int) -> Any:
        return self.http.request("DELETE", self._path(type_name, item_id)).data

    def delete_many(self, type_name: str, ids: Sequence[int]) -> list[int]:
        """Bulk delete, in batches of 100.

        Each batch is authorized as a whole server-side and deletes nothing if
        any id in it is off-limits — so a rejected id costs you its batch, not
        the ones already done.
        """

        deleted: list[int] = []

        for batch in chunked(list(ids), MAX_BULK_DELETE):
            response = self.http.request(
                "DELETE", self._path(type_name), json_body=list(batch)
            )

            payload = response.data

            if isinstance(payload, dict) and isinstance(payload.get("deleted"), list):
                deleted.extend(payload["deleted"])

        return deleted

    # -- relations -----------------------------------------------------------

    def relation_get(self, type_name: str, item_id: int, relation: str) -> Any:
        if self.http.anonymous:
            raise UsageError(
                "Relations need a key — the anonymous surface covers content "
                "items only, and never their relations.",
                hint="Drop --anon.",
            )

        self._check_relation(type_name, relation)

        return self.http.request(
            "GET", self._relation_path(type_name, item_id, relation)
        ).data

    def relation_write(
        self,
        method: str,
        type_name: str,
        item_id: int,
        relation: str,
        members: Sequence[Any],
    ) -> Any:
        """PUT (replace) or POST (merge) a relation.

        A PUT is deliberately NOT batched: the server reads the body as the
        complete set, so sending it in two halves would have the second half
        delete the first. A POST merges, so it batches safely.
        """

        self._check_relation(type_name, relation)

        method = method.upper()
        path = self._relation_path(type_name, item_id, relation)

        if method == "PUT" and len(members) > MAX_RELATION_MEMBERS:
            raise UsageError(
                f"A relation PUT replaces the whole set and is capped at "
                f"{MAX_RELATION_MEMBERS} members ({len(members)} given). "
                "Split it into a PUT of the first batch plus POSTs of the rest."
            )

        result: Any = None
        sent = False

        for batch in chunked(list(members), MAX_RELATION_MEMBERS):
            result = self.http.request(method, path, json_body=list(batch)).data
            sent = True

        # An empty body still has a meaning — `PUT []` clears the set — and
        # `chunked` yields nothing for an empty list, so that one case needs its
        # own request. Keyed on whether anything was SENT, not on the response
        # being null: a response whose `data` is null is still a response, and
        # testing that instead sent the whole request a second time.
        if not sent:
            result = self.http.request(method, path, json_body=[]).data

        return result

    def relation_remove(
        self, type_name: str, item_id: int, relation: str, keys: Sequence[Any] | None
    ) -> Any:
        """Remove named members, or the whole set when `keys` is None."""

        self._check_relation(type_name, relation)

        path = self._relation_path(type_name, item_id, relation)

        # No body clears the relation; the server treats an absent body and an
        # empty one differently, so this sends nothing at all rather than `[]`.
        if keys is None:
            return self.http.request("DELETE", path).data

        keys = list(keys)

        # An empty list is not "clear it" — that is `keys is None`, above — so
        # sending nothing would be a silent no-op where the server answers 400.
        if not keys:
            raise UsageError(
                "Name at least one member to remove.",
                hint="Pass no keys at all to clear the whole relation.",
            )

        # A DELETE names KEYS, not members, so it is bounded by the body cap
        # (500) rather than the element-schema cap (200) that PUT/POST meet
        # first. Batching is safe here in a way it is not for PUT: each request
        # removes what it names and leaves the rest alone.
        result: Any = None

        for batch in chunked(keys, MAX_RELATION_KEYS):
            result = self.http.request("DELETE", path, json_body=list(batch)).data

        return result

    # -- files ---------------------------------------------------------------

    def upload_files(
        self,
        paths: Sequence[str],
        *,
        title: str | None = None,
        description: str | None = None,
        release_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Upload files, 20 per request.

        Multipart always answers with an array, even for one file, so the return
        shape is uniform. `title`/`description` are only sent when there is
        exactly one file in the request — with several the server cannot tell
        which they belong to and each file keeps its own name instead.
        """

        uploaded: list[dict[str, Any]] = []
        batches = list(chunked(list(paths), MAX_UPLOAD_PARTS))

        for index, batch in enumerate(batches, start=1):
            if len(batches) > 1:
                self._progress(f"uploading batch {index}/{len(batches)} ({len(batch)} files)")

            fields: dict[str, str] = {}

            if release_id is not None:
                fields["releaseId"] = str(release_id)

            if len(batch) == 1:
                if title is not None:
                    fields["title"] = title

                if description is not None:
                    fields["description"] = description

            response = self.http.request(
                "POST",
                "/api/content/file",
                files=[FilePart(path=str(p)) for p in batch],
                fields=fields,
            )

            payload = response.data

            if isinstance(payload, list):
                uploaded.extend(payload)
            elif isinstance(payload, dict):
                uploaded.append(payload)

        return uploaded

    def upload_raw(
        self,
        path: str,
        *,
        name: str,
        content_type: str | None = None,
        title: str | None = None,
        description: str | None = None,
        release_id: int | None = None,
    ) -> Any:
        """The raw-bytes form: the body IS the file. Answers with one object."""

        with open(path, "rb") as handle:
            body = handle.read()

        params: dict[str, Any] = {"name": name}

        if title is not None:
            params["title"] = title

        if description is not None:
            params["description"] = description

        if release_id is not None:
            params["releaseId"] = release_id

        return self.http.request(
            "POST",
            "/api/content/file",
            params=params,
            raw_body=body,
            content_type=content_type,
        ).data

    def file_get(self, file_id: str) -> Any:
        # `/api/content/file` is its own route and never reaches the anonymous
        # branch, so without this the answer is a bare 401 that reads as "your
        # key is wrong" rather than "files are not a public surface".
        if self.http.anonymous:
            raise UsageError(
                "Files always need a key — they are owned rows, not public "
                "content items.",
                hint="Drop --anon.",
            )

        return self.http.request("GET", f"/api/content/file/{file_id}").data

    def file_update(
        self, file_id: str, *, title: Any = ..., description: Any = ...
    ) -> Any:
        payload: dict[str, Any] = {}

        # `...` means "not given"; an explicit None clears the column, which is
        # a different request from omitting the field.
        if title is not ...:
            payload["title"] = title

        if description is not ...:
            payload["description"] = description

        if not payload:
            raise UsageError("Nothing to update — pass --title and/or --description.")

        return self.http.request(
            "PUT", f"/api/content/file/{file_id}", json_body=payload
        ).data

    def file_delete(self, file_id: str) -> Any:
        return self.http.request("DELETE", f"/api/content/file/{file_id}").data

    # -- internals -----------------------------------------------------------

    def _batched_write(
        self,
        method: str,
        type_name: str,
        payloads: Sequence[dict[str, Any]],
        size: int,
        verb: str,
    ) -> BulkResult:
        result = BulkResult()
        batches = list(chunked(list(payloads), size))
        offset = 0

        for index, batch in enumerate(batches, start=1):
            if len(batches) > 1:
                self._progress(
                    f"{verb} batch {index}/{len(batches)} ({len(batch)} items)"
                )

            try:
                response = self.http.request(
                    method, self._path(type_name), json_body=list(batch)
                )
            except ApiError as err:
                # The server returns what it managed to write before the failure.
                if isinstance(err.data, list):
                    result.items.extend(err.data)

                result.error = err
                result.failed_index = (
                    offset + err.index if err.index is not None else None
                )

                return result

            payload = response.data

            if isinstance(payload, list):
                result.items.extend(payload)
            elif payload is not None:
                result.items.append(payload)

            offset += len(batch)

        return result

    def _check_anonymous_type(self, type_name: str) -> None:
        """Anonymous reads cover the seven content items and nothing else."""

        if not self.http.anonymous or type_name in ANON_TYPES:
            return

        raise UsageError(
            f"'{type_name}' cannot be read without a key.",
            hint=(
                "The anonymous surface covers "
                f"{', '.join(ANON_TYPES)}. Drop --anon for anything else."
            ),
        )

    def _anon_list_params(
        self, type_name: str, params: dict[str, Any], mine: bool
    ) -> dict[str, Any]:
        """Strip what an anonymous listing does not implement, and say so.

        Silence would be the wrong answer here. The server ignores an unknown
        query param rather than refusing it, so `--search rust --anon` would come
        back as an unfiltered page of the newest mods — which looks exactly like
        a search that matched everything.
        """

        self._check_anonymous_type(type_name)

        kept: dict[str, Any] = {}
        dropped: list[str] = []

        for name, value in params.items():
            if name not in ANON_LIST_FILTERS:
                dropped.append(name)
                continue

            if name == "appId" and type_name not in ANON_APP_FILTER_TYPES:
                dropped.append(name)
                continue

            kept[name] = value

        if mine:
            dropped.append("mine")

        if dropped:
            # `warn`, not progress: this is the same class of thing as an
            # unsupported filter on the keyed list, and it changes what the
            # answer means rather than just how long it takes.
            output.warn(
                "anonymous listings take no "
                + ", ".join(sorted(dropped))
                + " — ignored."
            )

        return kept

    def _path(self, type_name: str, item_id: int | None = None) -> str:
        if type_name not in TYPES:
            raise UsageError(
                f"Unknown content type '{type_name}'.",
                hint=f"Known types: {', '.join(TYPES)}",
            )

        base = f"/api/content/{type_name}"

        return f"{base}/{item_id}" if item_id is not None else base

    def _relation_path(self, type_name: str, item_id: int, relation: str) -> str:
        return f"/api/content/{type_name}/{item_id}/{relation}"

    def _check_relation(self, type_name: str, relation: str) -> None:
        if relation not in RELATIONS:
            raise UsageError(
                f"Unknown relation '{relation}'.",
                hint=f"Known relations: {', '.join(RELATIONS)}",
            )

        available = relations_for(type_name)

        if relation not in available:
            raise UsageError(
                f"'{type_name}' items have no '{relation}' relation.",
                hint=f"Available for {type_name}: {', '.join(available) or 'none'}",
            )


def iter_ids(values: Iterable[Any]) -> list[int]:
    """Read ids out of a mixed list of ints, numeric strings and row dicts."""

    ids: list[int] = []

    for value in values:
        if isinstance(value, dict):
            value = value.get("id")

        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            raise UsageError(f"'{value}' is not an item id.") from None

    return ids
