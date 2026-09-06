"""`tmc rel …`, plus the shortcuts for the relations people touch daily.

THE ONE THING TO INTERNALISE
----------------------------
`PUT` on a relation is the COMPLETE set — anything you don't list is deleted.
`POST` merges by identity and leaves the rest alone. That distinction is the
whole reason the sub-resource exists (an inline `"media": [...]` on the item body
can only ever mean "replace"), so the verbs here are named for the intent rather
than the method:

    tmc rel add    → POST   (append / update the ones you named)
    tmc rel set    → PUT    (this is now the whole set)
    tmc rel rm     → DELETE (drop the ones you named)
    tmc rel clear  → DELETE (drop all of them)

Identity is `id` for most relations, `sourceId` for sources, and the folded name
for tags — which is what `rm` matches on.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .. import output
from ..context import Context
from ..errors import UsageError
from ..params import load_json_file, parse_json_arg, require_list
from ..schema import RELATIONS, relations_for


def _target(ctx: Context) -> tuple[str, int, str]:
    args = ctx.args

    return args.type, args.id, args.relation


def _columns(relation: str) -> tuple[str, ...]:
    spec = RELATIONS.get(relation)

    return spec.columns if spec else ()


# ---- generic ----------------------------------------------------------------


def rel_get(ctx: Context) -> int:
    type_name, item_id, relation = _target(ctx)

    members = ctx.client.relation_get(type_name, item_id, relation)

    ctx.emit(members, columns=_columns(relation))

    return 0


def rel_add(ctx: Context) -> int:
    return _rel_write(ctx, "POST", "added to")


def rel_set(ctx: Context) -> int:
    type_name, item_id, relation = _target(ctx)

    if not ctx.confirm(
        f"Replace the entire '{relation}' set on {type_name} #{item_id}? "
        "Anything not in the new set is deleted."
    ):
        ctx.note("Aborted.")
        return 1

    return _rel_write(ctx, "PUT", "replaced on")


def _rel_write(ctx: Context, method: str, verb: str) -> int:
    type_name, item_id, relation = _target(ctx)

    members = _collect_members(ctx, relation)

    if not members and method == "POST":
        raise UsageError(
            "Nothing to add.",
            hint="Pass members positionally, --member '<json>', or --from-file members.json.",
        )

    result = ctx.client.relation_write(method, type_name, item_id, relation, members)

    ctx.done(f"{relation} {verb} {type_name} #{item_id} ({len(members)} sent)")
    ctx.emit(result, columns=_columns(relation))

    return 0


def rel_rm(ctx: Context) -> int:
    type_name, item_id, relation = _target(ctx)

    keys: list[Any] = []

    for raw in ctx.args.keys:
        # Row ids are integers on the wire; tag names are strings. Sending "12"
        # where 12 is meant simply fails to match, so coerce what looks numeric.
        keys.append(int(raw) if raw.lstrip("-").isdigit() else raw)

    if not keys:
        raise UsageError(
            "Name at least one member to remove.",
            hint=f"Use 'tmc rel clear {type_name} {item_id} {relation}' to remove them all.",
        )

    result = ctx.client.relation_remove(type_name, item_id, relation, keys)

    ctx.done(f"Removed {len(keys)} member(s) from {type_name} #{item_id} {relation}")
    ctx.emit(result, columns=_columns(relation))

    return 0


def rel_clear(ctx: Context) -> int:
    type_name, item_id, relation = _target(ctx)

    if not ctx.confirm(f"Remove every '{relation}' member from {type_name} #{item_id}?"):
        ctx.note("Aborted.")
        return 1

    result = ctx.client.relation_remove(type_name, item_id, relation, None)

    ctx.done(f"Cleared {relation} on {type_name} #{item_id}")
    ctx.emit(result, columns=_columns(relation))

    return 0


def _collect_members(ctx: Context, relation: str) -> list[Any]:
    """Members from every input form: positional, --member, --from-file."""

    args = ctx.args
    members: list[Any] = []

    if getattr(args, "from_file", None):
        members.extend(require_list(load_json_file(args.from_file), args.from_file))

    for raw in getattr(args, "member", None) or []:
        members.append(parse_json_arg(raw))

    for raw in getattr(args, "members", None) or []:
        # Tags are plain strings; everything else takes a JSON object, and a
        # bare word there would be a confusing 400 rather than a clear refusal.
        if relation == "tags":
            members.append(raw)
        elif raw.lstrip().startswith(("{", "[")):
            members.append(parse_json_arg(raw))
        else:
            raise UsageError(
                f"'{raw}' is not a valid '{relation}' member.",
                hint=f"Expected {RELATIONS[relation].member}",
            )

    return members


# ---- tags -------------------------------------------------------------------


def tags_list(ctx: Context) -> int:
    tags = ctx.client.relation_get(ctx.args.type, ctx.args.id, "tags")

    if getattr(ctx.args, "output", "table") == "table":
        for tag in tags or []:
            print(tag)
    else:
        ctx.emit(tags)

    return 0


def tags_add(ctx: Context) -> int:
    result = ctx.client.relation_write(
        "POST", ctx.args.type, ctx.args.id, "tags", list(ctx.args.names)
    )

    ctx.done(f"Tagged {ctx.args.type} #{ctx.args.id}: {', '.join(ctx.args.names)}")
    _print_tags(ctx, result)

    return 0


def tags_set(ctx: Context) -> int:
    result = ctx.client.relation_write(
        "PUT", ctx.args.type, ctx.args.id, "tags", list(ctx.args.names)
    )

    ctx.done(f"Tags on {ctx.args.type} #{ctx.args.id} are now: {', '.join(ctx.args.names)}")
    _print_tags(ctx, result)

    return 0


def tags_rm(ctx: Context) -> int:
    result = ctx.client.relation_remove(
        ctx.args.type, ctx.args.id, "tags", list(ctx.args.names)
    )

    ctx.done(f"Untagged {ctx.args.type} #{ctx.args.id}: {', '.join(ctx.args.names)}")
    _print_tags(ctx, result)

    return 0


def _print_tags(ctx: Context, tags: Any) -> None:
    if ctx.quiet:
        return

    if getattr(ctx.args, "output", "table") != "table":
        ctx.emit(tags)
        return

    output.info(f"  now: {', '.join(tags) if tags else '(none)'}")


# ---- media ------------------------------------------------------------------


def media_list(ctx: Context) -> int:
    members = ctx.client.relation_get(ctx.args.type, ctx.args.id, "media")

    ctx.emit(members, columns=_columns("media"))

    return 0


def media_add(ctx: Context) -> int:
    """Attach gallery entries — uploading local files on the way through.

    A gallery entry is either an uploaded file or an external URL, and the
    server checks that any `fileId` belongs to you. Passing `--file shot.png`
    therefore means "upload this, then attach it", which is two API calls the
    caller should not have to sequence by hand.
    """

    args = ctx.args
    members: list[dict[str, Any]] = []

    if args.file:
        paths = [os.path.expanduser(path) for path in args.file]

        for path in paths:
            if not os.path.isfile(path):
                raise UsageError(f"No such file: {path}")

        ctx.progress(f"uploading {len(paths)} file(s)")

        uploaded = ctx.client.upload_files(
            paths,
            title=args.title if len(paths) == 1 else None,
            description=args.description if len(paths) == 1 else None,
        )

        for index, row in enumerate(uploaded):
            member: dict[str, Any] = {"fileId": row.get("id")}

            if args.type_ is not None:
                member["type"] = args.type_

            if args.title and len(uploaded) == 1:
                member["title"] = args.title
            elif row.get("title"):
                member["title"] = row["title"]

            if args.description and len(uploaded) == 1:
                member["description"] = args.description

            members.append(member)

    for url in args.url or []:
        member = {"externalUrl": url}

        if args.type_ is not None:
            member["type"] = args.type_

        if args.title:
            member["title"] = args.title

        if args.description:
            member["description"] = args.description

        members.append(member)

    for raw in args.member or []:
        members.append(parse_json_arg(raw))

    if args.from_file:
        members.extend(require_list(load_json_file(args.from_file), args.from_file))

    if not members:
        raise UsageError(
            "Nothing to add.",
            hint="Pass --file shot.png, --url https://…, or --member '<json>'.",
        )

    result = ctx.client.relation_write(
        "POST", args.type, args.id, "media", members
    )

    ctx.done(f"Added {len(members)} media item(s) to {args.type} #{args.id}")
    ctx.emit(result, columns=_columns("media"))

    return 0


def media_rm(ctx: Context) -> int:
    result = ctx.client.relation_remove(
        ctx.args.type, ctx.args.id, "media", [int(v) for v in ctx.args.ids]
    )

    ctx.done(f"Removed {len(ctx.args.ids)} media item(s)")
    ctx.emit(result, columns=_columns("media"))

    return 0


# ---- links ------------------------------------------------------------------


def links_list(ctx: Context) -> int:
    members = ctx.client.relation_get(ctx.args.type, ctx.args.id, "links")

    ctx.emit(members, columns=_columns("links"))

    return 0


def links_add(ctx: Context) -> int:
    args = ctx.args

    members = [
        {"url": url, **({"type": args.link_type} if args.link_type else {})}
        for url in args.urls
    ]

    result = ctx.client.relation_write("POST", args.type, args.id, "links", members)

    ctx.done(f"Added {len(members)} link(s) to {args.type} #{args.id}")
    ctx.emit(result, columns=_columns("links"))

    return 0


def links_rm(ctx: Context) -> int:
    result = ctx.client.relation_remove(
        ctx.args.type, ctx.args.id, "links", [int(v) for v in ctx.args.ids]
    )

    ctx.done(f"Removed {len(ctx.args.ids)} link(s)")
    ctx.emit(result, columns=_columns("links"))

    return 0


# ---- introspection ----------------------------------------------------------


def rel_describe(type_name: str) -> str:
    available = relations_for(type_name)

    if not available:
        return f"{type_name} has no relation sub-resources."

    lines = [f"{type_name} relations:"]

    for name in available:
        spec = RELATIONS[name]
        lines.append(f"  {name:<9} member: {spec.member}")
        lines.append(f"  {'':<9} identity: {spec.key}")

    return "\n".join(lines)


def dump_member_template(relation: str) -> str:
    """A skeleton member, so `--from-file` has an obvious starting point."""

    templates: dict[str, Any] = {
        "tags": ["tag-one", "tag-two"],
        "media": [
            {"externalUrl": "https://example.com/shot.png", "title": "Screenshot"},
            {"fileId": "<file id>", "type": "IMAGE", "title": "Uploaded"},
        ],
        "releases": [
            {
                "version": "1.0.0",
                "title": "First release",
                "description": "What changed",
                "files": ["<file id>"],
            }
        ],
        "links": [{"type": "GITHUB", "url": "https://github.com/you/repo"}],
        "sources": [{"sourceId": 1, "path": "/mods/my-mod"}],
        "items": [{"modId": 5, "title": "A mod worth having"}],
    }

    return json.dumps(templates.get(relation, []), indent=2)
