"""Download, verify, unpack and record. Mirrors `install.rs`, minus the app's database.

Where it lands: the app keeps its local mods in a SQLite library this tool does
not write (a second writer would corrupt it). So the CLI installs into a store
of its own, `external-mods/` under the data dir (`--dest` / `TMC_MODS_DIR`),
with one `external-mods.json` manifest, and that manifest is what `remove` and
`list` read. Nothing in the app reads it yet; `docs/external-mods.md` says so.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.parse
import zipfile
from dataclasses import dataclass
from typing import Any, Callable

from ..errors import CliError
from .resolve import Fetch, ProviderError, RemoteFile, Resolved

MANIFEST = "external-mods.json"

#: The nxm grant is single-use and personal; like `stored_url` in install.rs it is
#: never written down. Anything that smells like a credential goes with it.
SECRET_PARAMS = ("key", "expires", "user_id", "token", "apikey", "api_key", "access_token", "signature", "sig")


def store_root(dest: str | None = None) -> str:
    if dest:
        return os.path.abspath(os.path.expanduser(dest))

    env = os.environ.get("TMC_MODS_DIR")

    if env:
        return os.path.abspath(os.path.expanduser(env))

    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")

    return os.path.join(base, "tmc", "external-mods")


def stored_url(raw: str) -> str:
    try:
        u = urllib.parse.urlsplit(raw)
    except ValueError:
        return raw

    if not u.query:
        return raw

    kept = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True) if k.lower() not in SECRET_PARAMS]

    return urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(kept), fragment=""))


def safe_name(raw: str) -> str:
    """One path component, never `..`: the provider names the file, we do not trust it."""

    name = re.split(r"[\\/]", raw)[-1].strip()

    if not name or name in (".", "..") or "\0" in name:
        raise ProviderError(f"the provider named a file {raw!r}, which cannot be saved")

    return name


def _slug(raw: str) -> str:
    out = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")

    return out[:80] or "x"


def _digest(path: str, algo: str) -> str:
    h = hashlib.new(algo)

    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)

    return h.hexdigest()


def verify_file(path: str, size: int | None, hashes: dict[str, str]) -> str:
    """Return how it was checked: sha512, sha256, size or none. Raises on a mismatch."""

    actual = os.path.getsize(path)

    if size is not None and size != actual:
        raise ProviderError(f"the download is {actual} bytes but the provider says {size}; it was discarded")

    for algo in ("sha512", "sha256"):
        want = (hashes.get(algo) or "").strip().lower()

        if want:
            if _digest(path, algo) != want:
                raise ProviderError(f"the download does not match the provider's {algo}; it was discarded")

            return algo

    return "size" if size is not None else "none"


def safe_extract(archive: str, dest: str) -> list[str]:
    """Unzip with zip-slip and symlink refusal. Returns the relative file list."""

    root = os.path.realpath(dest)
    out: list[str] = []

    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            target = os.path.realpath(os.path.join(root, name))

            if os.path.isabs(name) or ".." in name.split("/") or not (target == root or target.startswith(root + os.sep)):
                raise ProviderError(f"the archive holds an unsafe path ({info.filename!r}); nothing was installed")

            if stat.S_ISLNK(info.external_attr >> 16):
                raise ProviderError(f"the archive holds a symlink ({info.filename!r}); nothing was installed")

            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue

            os.makedirs(os.path.dirname(target), exist_ok=True)

            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)

            out.append(name)

    return sorted(out)


# -- manifest ----------------------------------------------------------------------


def load_manifest(root: str) -> dict[str, Any]:
    path = os.path.join(root, MANIFEST)

    if not os.path.exists(path):
        return {"version": 1, "mods": []}

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as err:
        raise CliError(f"cannot read {path}: {err}", hint="Fix or delete the file; installed mods stay on disk.") from None

    data.setdefault("mods", [])

    return data


def save_manifest(root: str, data: dict[str, Any]) -> None:
    os.makedirs(root, exist_ok=True)
    tmp = os.path.join(root, MANIFEST + ".tmp")

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    os.replace(tmp, os.path.join(root, MANIFEST))


def mod_id(provider: str, project: str) -> str:
    return f"{provider}:{project}"


def find(data: dict[str, Any], query: str) -> dict[str, Any] | None:
    """By id, by `provider:project`, or by a unique title (case-insensitive)."""

    mods = data["mods"]

    for m in mods:
        if m["id"] == query:
            return m

    hits = [m for m in mods if m["title"].lower() == query.lower() or m["projectId"] == query]

    return hits[0] if len(hits) == 1 else None


# -- the install -------------------------------------------------------------------


def pick_file(resolved: Resolved, name: str | None) -> RemoteFile:
    if name:
        for f in resolved.files:
            if f.name == name:
                return f

        raise ProviderError(f"no file named {name!r}; the choices are: " + ", ".join(f.name for f in resolved.files))

    for f in resolved.files:
        if f.primary:
            return f

    return resolved.files[0]


@dataclass
class Installed:
    id: str
    path: str
    files: list[str]
    verified: str
    replaced: bool


def install(
    resolved: Resolved,
    file: RemoteFile,
    source_url: str,
    fetch: Fetch,
    root: str,
    *,
    max_bytes: int,
    progress: Callable[[int, int | None], None] | None = None,
) -> Installed:
    name = safe_name(file.name)

    if max_bytes and file.size and file.size > max_bytes:
        raise ProviderError(f"{name} is {file.size // (1024 * 1024)} MB, past the {max_bytes // (1024 * 1024)} MB limit (--max-mb; 0 means no limit)")

    os.makedirs(root, exist_ok=True)
    work = tempfile.mkdtemp(prefix=".dl-", dir=root)

    try:
        part = os.path.join(work, name)
        fetch.download(file.url, part, max_bytes=max_bytes, progress=progress)
        verified = verify_file(part, file.size, file.hashes)

        mid = mod_id(resolved.provider, resolved.project_id)
        final = os.path.join(root, _slug(resolved.provider), _slug(resolved.project_id), _slug(resolved.version_id or "current"))
        stage = os.path.join(work, "out")
        os.makedirs(stage)

        if name.lower().endswith(".zip") and zipfile.is_zipfile(part):
            files = safe_extract(part, stage)
        else:
            shutil.move(part, os.path.join(stage, name))
            files = [name]

        data = load_manifest(root)
        old = find(data, mid)

        if os.path.exists(final):
            shutil.rmtree(final)

        os.makedirs(os.path.dirname(final), exist_ok=True)
        shutil.move(stage, final)

        if old and os.path.realpath(old["path"]) != os.path.realpath(final):
            _remove_dir(root, old["path"])

        data["mods"] = [m for m in data["mods"] if m["id"] != mid]
        data["mods"].append({
            "id": mid, "provider": resolved.provider, "projectId": resolved.project_id,
            "versionId": resolved.version_id, "version": resolved.version, "title": resolved.title,
            "url": stored_url(source_url), "path": final, "files": files, "verified": verified,
            "installedAt": int(time.time()),
        })
        save_manifest(root, data)

        return Installed(mid, final, files, verified, old is not None)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _remove_dir(root: str, path: str) -> None:
    """Delete a mod's directory only when it is inside the store (a hand-edited manifest must not point rm elsewhere)."""

    real, base = os.path.realpath(path), os.path.realpath(root)

    if real != base and real.startswith(base + os.sep):
        shutil.rmtree(real, ignore_errors=True)


def remove(root: str, entry: dict[str, Any]) -> None:
    _remove_dir(root, entry["path"])
    data = load_manifest(root)
    data["mods"] = [m for m in data["mods"] if m["id"] != entry["id"]]
    save_manifest(root, data)
