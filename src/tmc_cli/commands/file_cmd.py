"""`tmc file …` — the FileUpload model, which is what releases and media hang off.

Uploads go through the API rather than a presigned S3 PUT: the server takes the
bytes itself, so there is no credential to hand out and nothing to sign locally.
The practical consequences the CLI has to handle are that the request is capped
at 20 file parts (so a bigger set is split automatically) and that the per-file
size limit belongs to the key's OWNER — 20 MB standard, 1 GB for supporters —
which we cannot read, so an oversized file is warned about locally and refused
authoritatively by the server with a 413.
"""

from __future__ import annotations

import glob as globlib
import os
import shutil
import urllib.request
from typing import Any

from .. import output
from ..context import Context
from ..errors import UsageError
from ..output import human_size
from ..schema import UPLOAD_LIMIT_SUPPORTER, UPLOAD_LIMIT_USER


def expand_paths(patterns: list[str]) -> list[str]:
    """Expand globs ourselves as well as letting the shell do it.

    A quoted pattern (`--file 'dist/*.zip'`) is the form that survives being
    written into a Makefile or a CI YAML, and it reaches us unexpanded.
    """

    paths: list[str] = []

    for pattern in patterns:
        expanded = os.path.expanduser(pattern)

        if os.path.isfile(expanded):
            paths.append(expanded)
            continue

        matches = sorted(globlib.glob(expanded, recursive=True))
        files = [m for m in matches if os.path.isfile(m)]

        if not files:
            raise UsageError(f"No files matched '{pattern}'.")

        paths.extend(files)

    # Deduplicate while keeping order: a glob and an explicit path often overlap,
    # and uploading the same file twice creates two rows.
    seen: set[str] = set()
    unique: list[str] = []

    for path in paths:
        real = os.path.realpath(path)

        if real in seen:
            continue

        seen.add(real)
        unique.append(path)

    return unique


def warn_on_size(paths: list[str]) -> None:
    for path in paths:
        size = os.path.getsize(path)

        if size == 0:
            raise UsageError(f"'{path}' is empty — the API rejects empty uploads.")

        if size > UPLOAD_LIMIT_SUPPORTER:
            raise UsageError(
                f"'{path}' is {human_size(size)}, above the 1 GB per-file ceiling."
            )

        if size > UPLOAD_LIMIT_USER:
            output.warn(
                f"{os.path.basename(path)} is {human_size(size)} — over the 20 MB "
                "standard-account limit. It will only be accepted if the key's "
                "owner is a supporter or above."
            )


def upload(ctx: Context) -> int:
    args = ctx.args

    paths = expand_paths(list(args.paths))
    warn_on_size(paths)

    total = sum(os.path.getsize(p) for p in paths)
    ctx.note(f"Uploading {len(paths)} file(s), {human_size(total)} total…")

    if args.raw:
        if len(paths) != 1:
            raise UsageError("--raw sends the body as one file; pass exactly one path.")

        row = ctx.client.upload_raw(
            paths[0],
            name=args.name or os.path.basename(paths[0]),
            content_type=args.content_type,
            title=args.title,
            description=args.description,
            release_id=args.release_id,
        )

        rows = [row] if isinstance(row, dict) else []
    else:
        rows = ctx.client.upload_files(
            paths,
            title=args.title,
            description=args.description,
            release_id=args.release_id,
        )

    ctx.done(f"Uploaded {len(rows)} file(s)")

    if args.release_id and not ctx.quiet:
        output.info(f"  attached to release #{args.release_id}")

    ctx.emit(rows, columns=("id", "title", "type", "size", "key"))

    return 0


def get(ctx: Context) -> int:
    row = ctx.client.file_get(ctx.args.id)

    ctx.emit(row, columns=("id", "title", "type", "size", "isPublic", "url", "key"))

    return 0


def update(ctx: Context) -> int:
    args = ctx.args

    row = ctx.client.file_update(
        args.id,
        title=args.title if args.title is not None else ...,
        description=args.description if args.description is not None else ...,
    )

    ctx.done(f"Updated file {args.id}")
    ctx.emit(row, columns=("id", "title", "type", "size"))

    return 0


def remove(ctx: Context) -> int:
    ids = list(ctx.args.ids)

    if not ctx.confirm(
        f"Delete {len(ids)} file(s)? The S3 object goes with the row."
    ):
        ctx.note("Aborted.")
        return 1

    deleted = []

    for file_id in ids:
        ctx.client.file_delete(file_id)
        deleted.append(file_id)

    ctx.done(f"Deleted {len(deleted)} file(s)")
    ctx.emit({"deleted": deleted})

    return 0


def download(ctx: Context) -> int:
    """Fetch a file's bytes via the CDN URL its metadata carries."""

    args = ctx.args
    row = ctx.client.file_get(args.id)

    if not isinstance(row, dict):
        raise UsageError(f"Unexpected response for file {args.id}.")

    url = row.get("url")

    if not url:
        raise UsageError(
            f"File {args.id} has no download URL.",
            hint="The site has no CDN configured, so there is nothing to fetch directly.",
        )

    if url.startswith("/"):
        # A private upload answers with a site-relative path that authorizes a
        # browser SESSION before signing — an API key cannot open it.
        raise UsageError(
            f"File {args.id} is private and is served from {ctx.settings.base_url}{url}, "
            "which needs a signed-in session rather than an API key."
        )

    destination = args.output_path or row.get("title") or f"{args.id}.bin"

    if os.path.isdir(destination):
        destination = os.path.join(destination, row.get("title") or f"{args.id}.bin")

    ctx.progress(f"downloading {url}")

    with urllib.request.urlopen(url, timeout=ctx.settings.timeout) as response:
        with open(destination, "wb") as handle:
            shutil.copyfileobj(response, handle)

    size = os.path.getsize(destination)

    ctx.done(f"Saved {destination} ({human_size(size)})")

    return 0


def summarize(rows: list[dict[str, Any]]) -> str:
    total = sum(int(row.get("size") or 0) for row in rows)

    return f"{len(rows)} file(s), {human_size(total)}"
