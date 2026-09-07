"""`tmc <type> …` — list / get / create / update / delete for every content type.

One implementation, parameterised by the type registry, because the API is one
implementation too: `/api/content/{type}` is a single handler and the only thing
that varies between types is which fields validate and which filters apply.

THE CONVENIENCES WORTH KNOWING
------------------------------
- `--icon`, `--banner`, `--card` take a **local path** as readily as a file id.
  Given a path they upload it first and use the id, because "set my mod's icon"
  should not be two commands and a copy-paste of a UUID.
- `--from-file items.json` creates or updates a whole array, batched into the
  25-per-request cap automatically.
- `--all` on a list walks every page.
- A partial bulk failure prints what landed, since the API's bulk writes are
  explicitly not transactional.
"""

from __future__ import annotations

import os
from typing import Any

from .. import output
from ..client import BulkResult
from ..context import Context
from ..errors import UsageError
from ..params import build_payload, load_json_file, require_list
from ..schema import ANON_COLUMNS, TYPES, TypeSpec


def _spec(ctx: Context) -> TypeSpec:
    name = ctx.args.type

    spec = TYPES.get(name)

    if spec is None:
        raise UsageError(
            f"Unknown content type '{name}'.",
            hint=f"Known types: {', '.join(TYPES)}",
        )

    return spec


# ---- list -------------------------------------------------------------------


def list_items(ctx: Context) -> int:
    spec = _spec(ctx)
    args = ctx.args

    filters: dict[str, Any] = {}

    if args.search:
        filters["search"] = args.search

    if args.tag:
        filters["tags"] = args.tag

    if args.category:
        filters["categoryIds"] = args.category

    if args.community is not None:
        filters["communityId"] = args.community

    if args.nsfw is not None:
        filters["nsfw"] = 1 if args.nsfw else 0

    if getattr(args, "app", None) is not None:
        filters["appId"] = args.app

    if getattr(args, "official", None) is not None:
        # The server reads anything but `0`/`false`/`no` as true, so send the
        # two spellings it names rather than trusting Python's `True`.
        filters["official"] = 1 if args.official else 0

    unsupported = [
        name
        for name in ("search", "tags", "categoryIds", "communityId", "nsfw")
        if name in filters and name not in spec.list_filters
    ]

    # The server silently ignores a filter the type does not declare, which is
    # the one failure mode worth catching here: a filter that does nothing looks
    # exactly like a filter that matched everything.
    for name in unsupported:
        output.warn(f"'{name}' is not a filter on {spec.name} — it will be ignored.")

    # `appId` and `official` are the mirror image: they are the anonymous
    # listing's only two filters and are not filters on the keyed one at all.
    # The client drops what --anon cannot use and warns; this covers the other
    # direction.
    if "appId" in filters and not ctx.client.http.anonymous:
        output.warn(
            "the keyed list has no app filter — '--app' will be ignored. "
            "Pass --anon to use it, or filter on the results."
        )
        filters.pop("appId")

    if "official" in filters and not ctx.client.http.anonymous:
        output.warn(
            "the keyed list has no official filter — '--official' will be "
            "ignored. Pass --anon to use it, or filter on the results."
        )
        filters.pop("official")

    rows, pagination = ctx.client.list(
        spec.name,
        page=args.page,
        limit=args.limit,
        mine=args.mine,
        filters=filters,
        all_pages=args.all,
        max_items=args.max,
    )

    ctx.emit(rows, columns=_columns(ctx, spec), pagination=pagination)

    return 0


def _columns(ctx: Context, spec: TypeSpec) -> tuple[str, ...]:
    """An anonymous read answers a summary, which has its own shape."""

    return ANON_COLUMNS if ctx.client.http.anonymous else spec.columns


# ---- get --------------------------------------------------------------------


def get_item(ctx: Context) -> int:
    spec = _spec(ctx)

    row = ctx.client.get(spec.name, ctx.args.id)

    ctx.emit(row, columns=_columns(ctx, spec))

    return 0


# ---- create / update --------------------------------------------------------


def create_item(ctx: Context) -> int:
    spec = _spec(ctx)
    args = ctx.args

    if args.from_file:
        return _bulk_create(ctx, spec, args.from_file)

    payload = _payload(ctx, spec)

    missing = [name for name in spec.required if name not in payload]

    if missing and not args.allow_unknown_fields:
        raise UsageError(
            f"Creating a {spec.name} needs {', '.join(spec.required)} — "
            f"missing {', '.join(missing)}.",
            hint=f"e.g. --set {missing[0]}=…",
        )

    row = ctx.client.create(spec.name, payload)

    ctx.done(f"Created {spec.name} #{_id_of(row)}")
    ctx.emit(row, columns=spec.columns)

    return 0


def update_item(ctx: Context) -> int:
    spec = _spec(ctx)
    args = ctx.args

    if args.from_file:
        return _bulk_update(ctx, spec, args.from_file)

    payload = _payload(ctx, spec)

    if not payload:
        raise UsageError(
            "Nothing to update.",
            hint="Pass --set field=value, --json '{...}' or --from-file items.json.",
        )

    _warn_about_relation_replacement(payload)

    row = ctx.client.update(spec.name, args.id, payload)

    ctx.done(f"Updated {spec.name} #{args.id}")
    ctx.emit(row, columns=spec.columns)

    return 0


def _payload(ctx: Context, spec: TypeSpec) -> dict[str, Any]:
    args = ctx.args

    payload = build_payload(
        spec,
        sets=args.set,
        set_jsons=args.set_json,
        set_files=args.set_file,
        json_body=args.json,
        json_file=None,
        allow_unknown=args.allow_unknown_fields,
    )

    if args.tag:
        payload["tags"] = list(args.tag)

    if getattr(args, "content_file", None):
        # `content` is a markdown body on every type that has one, and nobody
        # wants to quote one of those into a shell.
        payload["content"] = _read(args.content_file)

    for flag, field_name in spec.image_fields.items():
        value = getattr(args, flag, None)

        if value is None:
            continue

        payload[field_name] = _resolve_image(ctx, value)

    return payload


def _resolve_image(ctx: Context, value: str) -> str | None:
    """A local path becomes an upload; anything else is already a file id."""

    if value == "":
        return None

    path = os.path.expanduser(value)

    if not os.path.isfile(path):
        return value

    ctx.progress(f"uploading {os.path.basename(path)}")

    uploaded = ctx.client.upload_files([path])

    if not uploaded:
        raise UsageError(f"Upload of '{path}' returned nothing.")

    file_id = uploaded[0].get("id")

    ctx.note(f"  {os.path.basename(path)} → file {file_id}")

    return str(file_id)


def _warn_about_relation_replacement(payload: dict[str, Any]) -> None:
    """An inline relation array is the COMPLETE set — say so before it lands."""

    replaced = [
        name
        for name in ("tags", "media", "releases", "links", "sourceItems", "items")
        if name in payload
    ]

    for name in replaced:
        output.warn(
            f"'{name}' on the item body replaces the whole relation "
            f"(anything not listed is deleted). Use 'tmc rel add' to append instead."
        )


# ---- bulk -------------------------------------------------------------------


def _bulk_create(ctx: Context, spec: TypeSpec, path: str) -> int:
    items = require_list(load_json_file(path), path)

    if not items:
        raise UsageError(f"{path} contains an empty array.")

    ctx.note(f"Creating {len(items)} {spec.name}(s)…")

    result = ctx.client.create_many(spec.name, items)

    return _report_bulk(ctx, spec, result, "created")


def _bulk_update(ctx: Context, spec: TypeSpec, path: str) -> int:
    items = require_list(load_json_file(path), path)

    if not items:
        raise UsageError(f"{path} contains an empty array.")

    ctx.note(f"Updating {len(items)} {spec.name}(s)…")

    result = ctx.client.update_many(spec.name, items)

    return _report_bulk(ctx, spec, result, "updated")


def _report_bulk(
    ctx: Context, spec: TypeSpec, result: BulkResult, verb: str
) -> int:
    if result.ok:
        ctx.done(f"{verb.capitalize()} {len(result.items)} {spec.name}(s)")
        ctx.emit(result.items, columns=spec.columns)

        return 0

    # Partial success is the normal failure mode here, so lead with what landed.
    output.error(result.error.format())

    if result.failed_index is not None:
        output.error(f"Failed at element {result.failed_index} of the input file.")

    if result.items:
        output.warn(
            f"{len(result.items)} item(s) were {verb} before the failure — "
            "re-running the whole file would duplicate them."
        )
        ctx.emit(result.items, columns=spec.columns)

    return result.error.exit_code


# ---- delete -----------------------------------------------------------------


def delete_items(ctx: Context) -> int:
    spec = _spec(ctx)
    ids = list(ctx.args.ids)

    if not ids:
        raise UsageError("Pass at least one id to delete.")

    label = f"{len(ids)} {spec.name}(s)" if len(ids) > 1 else f"{spec.name} #{ids[0]}"

    if not ctx.confirm(f"Delete {label}? This cannot be undone."):
        ctx.note("Aborted.")
        return 1

    if len(ids) == 1:
        ctx.client.delete(spec.name, ids[0])
        ctx.done(f"Deleted {spec.name} #{ids[0]}")

        return 0

    deleted = ctx.client.delete_many(spec.name, ids)

    ctx.done(f"Deleted {len(deleted)} {spec.name}(s)")

    missing = sorted(set(ids) - set(deleted))

    if missing:
        # Bulk delete skips ids that do not exist rather than failing, so this is
        # the only place the caller learns which ones were already gone.
        output.warn(f"Not deleted (already gone or not visible): {', '.join(map(str, missing))}")

    ctx.emit({"deleted": deleted})

    return 0


# ---- helpers ----------------------------------------------------------------


def _read(path: str) -> str:
    from ..params import read_text_file

    return read_text_file(path)


def _id_of(row: Any) -> Any:
    return row.get("id") if isinstance(row, dict) else "?"
