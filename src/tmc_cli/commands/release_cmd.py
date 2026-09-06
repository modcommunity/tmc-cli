"""`tmc release publish` — the one command this CLI exists for.

Cutting a release by hand is four steps in a specific order: upload each file,
collect the ids, read the existing release set so you don't clobber it, then
write the release with its files attached. Every one of them is a place to get
it wrong, and two of them are wrong by DEFAULT if you're not careful:

  1. **An inline relation array is the complete set.** `PUT` on `releases` with
     one member deletes every other release the item has. So publish uses
     `POST`, which merges by id.
  2. **`hidden` is not preserved on an update.** The server's release sync
     writes `hidden: r.hidden ?? false`, so an update that omits the field
     un-hides a draft. Publish therefore reads the existing release first and
     sends it back whole, with only the fields you asked for changed.

Everything else — batching uploads past the 20-per-request cap, matching an
existing release by version, merging the file set rather than replacing it — is
the same "do the obvious thing" logic that would otherwise live in a shell
script on every machine that publishes.
"""

from __future__ import annotations

import os
from typing import Any

from .. import output
from ..context import Context
from ..errors import UsageError
from ..output import human_size
from .file_cmd import expand_paths, warn_on_size

#: The parent types that carry releases (`RELATION_DEFS.releases.types`).
RELEASE_PARENTS = ("asset", "mod", "server")


def resolve_parent(ctx: Context) -> tuple[str, int]:
    """Read `--mod 5` / `--asset 5` / `--server 5` into (type, id)."""

    chosen = [
        (name, getattr(ctx.args, name))
        for name in RELEASE_PARENTS
        if getattr(ctx.args, name, None) is not None
    ]

    if not chosen:
        raise UsageError(
            "Name the item the release belongs to.",
            hint="Pass one of --mod ID, --asset ID or --server ID.",
        )

    if len(chosen) > 1:
        raise UsageError(
            "A release belongs to exactly one item — pass only one of "
            f"{', '.join('--' + name for name, _ in chosen)}."
        )

    return chosen[0]


def publish(ctx: Context) -> int:
    args = ctx.args
    parent_type, parent_id = resolve_parent(ctx)

    # 1. What is already there. Needed both to find the release we may be
    #    updating and to preserve the fields the sync would otherwise reset.
    existing = ctx.client.relation_get(parent_type, parent_id, "releases") or []

    current = _find_release(existing, args.release_id, args.version)

    if current is not None:
        ctx.note(
            f"Updating release #{current['id']} ({current.get('version')}) "
            f"on {parent_type} #{parent_id}"
        )
    else:
        ctx.note(f"Creating release {args.version} on {parent_type} #{parent_id}")

    # 2. Upload the payload files, batching past the 20-per-request cap.
    file_ids: list[str] = list(args.file_id or [])

    if args.file:
        paths = expand_paths(list(args.file))
        warn_on_size(paths)

        total = sum(os.path.getsize(p) for p in paths)
        ctx.note(f"Uploading {len(paths)} file(s), {human_size(total)}…")

        uploaded = ctx.client.upload_files(paths)

        for row in uploaded:
            ctx.note(f"  {row.get('title')} → {row.get('id')} ({human_size(row.get('size') or 0)})")
            file_ids.append(str(row.get("id")))

    # 3. Decide the file set. Merging is the default because a release usually
    #    gains a build rather than being re-cut from scratch.
    previous_files = [str(f) for f in (current or {}).get("files", []) or []]

    if args.replace_files:
        files = file_ids
    else:
        files = previous_files + [fid for fid in file_ids if fid not in previous_files]

    # 4. Build the member. An existing release is sent back WHOLE — see the
    #    module docstring for why omitting `hidden` would un-hide a draft.
    member: dict[str, Any] = dict(current or {})
    member.pop("files", None)

    member["version"] = args.version or member.get("version")

    if not member["version"]:
        raise UsageError("A release needs a --version.")

    if args.title is not None:
        member["title"] = args.title

    if args.description is not None:
        member["description"] = args.description

    if args.content_file:
        from ..params import read_text_file

        member["content"] = read_text_file(args.content_file)
    elif args.content is not None:
        member["content"] = args.content

    if args.hidden is not None:
        member["hidden"] = args.hidden
    else:
        member.setdefault("hidden", False)

    if files or args.replace_files:
        member["files"] = files

    if ctx.settings.dry_run:
        output.info("Would POST this release member:")

    # POST merges by id: an existing release is replaced in place, a new one is
    # appended, and every other release on the item is left exactly as it was.
    result = ctx.client.relation_write(
        "POST", parent_type, parent_id, "releases", [member]
    )

    published = _find_release(result or [], member.get("id"), member["version"])

    ctx.done(
        f"Published {member['version']} on {parent_type} #{parent_id}"
        + (f" (release #{published['id']})" if published else "")
    )

    if published and not ctx.quiet:
        attached = published.get("files") or []
        output.info(f"  {len(attached)} file(s) attached")

        if published.get("hidden"):
            output.info("  release is hidden — it is a draft until you unhide it")

    ctx.emit(result, columns=("id", "version", "title", "hidden", "files"))

    return 0


def list_releases(ctx: Context) -> int:
    parent_type, parent_id = resolve_parent(ctx)

    releases = ctx.client.relation_get(parent_type, parent_id, "releases")

    ctx.emit(releases, columns=("id", "version", "title", "hidden", "files"))

    return 0


def remove(ctx: Context) -> int:
    args = ctx.args
    parent_type, parent_id = resolve_parent(ctx)

    releases = ctx.client.relation_get(parent_type, parent_id, "releases") or []
    target = _find_release(releases, args.release_id, args.version)

    if target is None:
        raise UsageError(
            f"No release matching "
            f"{'#' + str(args.release_id) if args.release_id else args.version} "
            f"on {parent_type} #{parent_id}."
        )

    label = f"#{target['id']} ({target.get('version')})"

    if not ctx.confirm(f"Delete release {label} from {parent_type} #{parent_id}?"):
        ctx.note("Aborted.")
        return 1

    result = ctx.client.relation_remove(
        parent_type, parent_id, "releases", [target["id"]]
    )

    ctx.done(f"Deleted release {label}")
    ctx.emit(result, columns=("id", "version", "title", "hidden", "files"))

    return 0


def files(ctx: Context) -> int:
    """The FileUpload rows attached to one release, resolved to full metadata."""

    args = ctx.args
    parent_type, parent_id = resolve_parent(ctx)

    releases = ctx.client.relation_get(parent_type, parent_id, "releases") or []
    target = _find_release(releases, args.release_id, args.version)

    if target is None:
        raise UsageError(f"No matching release on {parent_type} #{parent_id}.")

    rows = []

    for file_id in target.get("files") or []:
        # The relation only stores ids; the metadata (size, type, CDN URL) is a
        # read per file, which is why this is its own command and not part of
        # `release list`.
        rows.append(ctx.client.file_get(str(file_id)))

    ctx.emit(rows, columns=("id", "title", "type", "size", "url"))

    return 0


def _find_release(
    releases: Any, release_id: int | None, version: str | None
) -> dict[str, Any] | None:
    """Match by id when given, else by exact version string."""

    if not isinstance(releases, list):
        return None

    for row in releases:
        if not isinstance(row, dict):
            continue

        if release_id is not None and row.get("id") == release_id:
            return row

    if release_id is not None:
        return None

    if version is None:
        return None

    for row in releases:
        if isinstance(row, dict) and str(row.get("version")) == str(version):
            return row

    return None
