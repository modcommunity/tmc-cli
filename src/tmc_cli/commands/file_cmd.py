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
import urllib.parse
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

    # The URL is the server's answer, and urllib opens file:// and ftp:// too.
    if urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
        raise UsageError(f"File {args.id} has a download URL that is not http(s): {url}")

    # The title is whatever the UPLOADER typed, and it is used as a filename
    # when -O is not given. Taken as-is, a title of "../../.bashrc" or
    # "/home/you/.ssh/authorized_keys" wrote the download there.
    name = safe_filename(row.get("title"), f"{args.id}.bin")
    destination = args.output_path or name

    # Whether the final component came from the server rather than from -O. A
    # server-named file must not replace one that is already there: run from
    # $HOME, a title of ".bashrc" is one path component and passes every check
    # above, and in a shared directory a symlink planted under the expected
    # name would carry the write anywhere its owner can.
    server_named = not args.output_path

    if os.path.isdir(destination):
        destination = os.path.join(destination, name)
        server_named = True

    ctx.progress(f"downloading {url}")

    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0)

    if server_named and not args.force:
        flags |= os.O_EXCL
    else:
        flags |= os.O_TRUNC

    if server_named:
        # Even with --force, never write THROUGH a link named by the server.
        flags |= getattr(os, "O_NOFOLLOW", 0)

    try:
        fd = os.open(destination, flags, 0o666)
    except FileExistsError:
        raise UsageError(
            f"{destination} already exists; not overwriting it with a server-named download.",
            hint="Pass --force to replace it, or -O to choose the path yourself.",
        ) from None
    except OSError as err:
        raise UsageError(f"Cannot write {destination}: {err.strerror or err}") from None

    try:
        with os.fdopen(fd, "wb") as handle:
            with urllib.request.urlopen(url, timeout=ctx.settings.timeout) as response:
                shutil.copyfileobj(response, handle)
    except BaseException:
        # Half a file under the real name reads as a finished download.
        try:
            os.unlink(destination)
        except OSError:
            pass

        raise

    size = os.path.getsize(destination)

    ctx.done(f"Saved {destination} ({human_size(size)})")

    return 0


# Names Windows maps to a device whatever the extension ("NUL.txt" is NUL).
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{stem}{n}" for stem in ("COM", "LPT") for n in "123456789\u00b9\u00b2\u00b3"
}

# What one path component may occupy on every filesystem this runs on.
_NAME_MAX_BYTES = 255


def safe_filename(title: Any, fallback: str, *, windows: bool = os.name == "nt") -> str:
    """A server-supplied name reduced to one path component in the destination.

    Control characters go too: they are legal in a POSIX name, but the name is
    echoed to the terminal ("Saved …"), where an escape sequence is an
    instruction rather than text.
    """

    name = str(title or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join("_" if _is_control(c) else c for c in name).strip()

    if windows:
        # "C:evil" joined onto a directory is drive-relative — a way out of it —
        # and "name:stream" writes an alternate data stream. Trailing dots and
        # spaces are silently dropped by Win32, so "..." would become "".
        name = "".join("_" if c in '<>:"|?*' else c for c in name).rstrip(". ")

        if name.split(".", 1)[0].rstrip(" ").upper() in _WINDOWS_RESERVED:
            name = f"_{name}"

    if name in ("", ".", ".."):
        return fallback

    return _clip(name)


def _is_control(char: str) -> bool:
    code = ord(char)

    return code < 0x20 or 0x7F <= code < 0xA0


def _clip(name: str) -> str:
    """Shorten to the filesystem's name limit, keeping a short extension."""

    if len(name.encode("utf-8")) <= _NAME_MAX_BYTES:
        return name

    stem, dot, ext = name.rpartition(".")

    if not dot or not stem or len(ext.encode("utf-8")) > 16:
        stem, ext = name, ""

    suffix = f".{ext}" if ext else ""
    budget = _NAME_MAX_BYTES - len(suffix.encode("utf-8"))

    return stem.encode("utf-8")[:budget].decode("utf-8", "ignore") + suffix


def summarize(rows: list[dict[str, Any]]) -> str:
    total = sum(int(row.get("size") or 0) for row in rows)

    return f"{len(rows)} file(s), {human_size(total)}"
