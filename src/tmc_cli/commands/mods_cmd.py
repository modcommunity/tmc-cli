"""`tmc mods`: install mods from outside the site. See docs/external-mods.md.

A user typing a link into a terminal is the paste-and-drop case of the app's
rules, so it installs directly: no confirmation, `--dry-run` for the plan.
Nothing here ever prints a credential: nxm grants are dropped before anything
is stored or shown.
"""

from __future__ import annotations

import os
import shutil
import webbrowser
from typing import Any, Callable

from ..app_api import AppSession
from ..context import Context
from ..errors import CliError, OpenPageError, UnavailableError, UsageError
from ..external import install as store
from ..external import links
from ..external.parse import KEYED, Ref, parse_input
from ..external.resolve import (
    Fetch,
    HttpFetch,
    ProviderError,
    RemoteFile,
    Resolved,
    env_max_mb,
    map_site,
    resolve_free,
)

NOT_ENABLED = (
    "{name} is not enabled here: it needs a key that only the site holds, and the site "
    "reports it as off (or you are not signed in; run 'tmc auth app-login')."
)
NEXUS = (
    "Nexus Mods downloads need your own Nexus key, and the CLI does not hold one. "
    "Install it with the app, or download the file and run 'tmc mods add <file>'."
)

_NAMES = {"curseforge": "CurseForge", "nexus": "Nexus Mods", "modio": "mod.io"}


class _LocalFetch:
    """For `tmc mods add ./file.zip`: the same pipeline, the source is a file."""

    def get_json(self, url: str) -> Any:
        raise ProviderError("a local file has no metadata")

    def download(self, url: str, dest: str, *, max_bytes: int, progress: Callable[[int, int | None], None] | None) -> int:
        src = url[len("file://"):]
        size = os.path.getsize(src)

        if max_bytes and size > max_bytes:
            raise ProviderError(f"{os.path.basename(src)} is past the {max_bytes // (1024 * 1024)} MB limit (--max-mb; 0 means no limit)")

        shutil.copyfile(src, dest)

        return size


def _fetcher(ctx: Context) -> Fetch:
    # Tests (and nothing else) swap this through the module attribute.
    return HttpFetch()


def _max_bytes(ctx: Context) -> int:
    mb = getattr(ctx.args, "max_mb", None)

    if mb is None:
        mb = env_max_mb()

    if mb is not None and mb < 0:
        raise UsageError("--max-mb must be 0 (no limit) or a positive number")

    return (mb or 0) * 1024 * 1024


def keyed_enabled(ctx: Context, provider: str) -> bool:
    """Ask the site which keyed providers are on. Any failure means off, as in the app."""

    try:
        data = AppSession(ctx.args).request("GET", "/external/providers")
    except CliError:
        return False

    flags = data.get("providers") if isinstance(data, dict) else None

    return bool(isinstance(flags, dict) and flags.get(provider))


def resolve_keyed(ctx: Context, ref: Ref) -> Resolved:
    if ref.provider == "nexus":
        raise UnavailableError(NEXUS)

    name = _NAMES.get(ref.provider, ref.provider)

    if not keyed_enabled(ctx, ref.provider):
        raise UnavailableError(NOT_ENABLED.format(name=name))

    params = {"provider": ref.provider, "project": ref.project, "version": ref.version, "game": ref.game, "class": ref.kind}

    body = AppSession(ctx.args).request("GET", "/external/resolve", params={k: v for k, v in params.items() if v})

    return map_site(ref.provider, body)


def resolve(ctx: Context, ref: Ref, fetch: Fetch) -> Resolved:
    if ref.provider in KEYED:
        return resolve_keyed(ctx, ref)

    return resolve_free(ref, fetch)


def _local(path: str) -> tuple[Resolved, str]:
    full = os.path.abspath(os.path.expanduser(path))
    name = os.path.basename(full)
    stem = name.rsplit(".", 1)[0] or name
    res = Resolved(
        provider="local", project_id=stem, version_id="", title=stem,
        files=[RemoteFile(url="file://" + full, name=name, size=os.path.getsize(full), primary=True)],
    )

    return res, full


def add(ctx: Context) -> int:
    args = ctx.args
    source: str = args.source
    root = store.store_root(args.dest)
    cap = _max_bytes(ctx)

    if os.path.isfile(os.path.expanduser(source)):
        resolved, shown = _local(source)
        fetch: Fetch = _LocalFetch()
    else:
        ref = parse_input(source)
        fetch = _fetcher(ctx)
        shown = store.stored_url(ref.url)
        resolved = resolve(ctx, ref, fetch)

    if resolved.manual_only:
        ctx.note(f"{resolved.title}: nothing to download here; the page is {resolved.manual_only}")

        if getattr(args, "open", False) and not ctx.dry_run:
            webbrowser.open(resolved.manual_only)

        raise OpenPageError(f"{resolved.title} is installed from its page, not by the CLI: {resolved.manual_only}")

    if not resolved.files:
        raise ProviderError(f"{resolved.title} has no downloadable file")

    chosen = store.pick_file(resolved, getattr(args, "file", None))
    plan = {
        "id": store.mod_id(resolved.provider, resolved.project_id),
        "title": resolved.title,
        "version": resolved.version,
        "file": chosen.name,
        "size": chosen.size,
        "from": shown,
        "into": root,
        "dependencies": ", ".join(resolved.deps),
    }

    if ctx.dry_run:
        plan["dryRun"] = True
        ctx.emit(plan)
        ctx.note("dry run: nothing was downloaded or written")

        return 0

    if resolved.deps:
        ctx.note("dependencies are not installed by the CLI: " + ", ".join(resolved.deps))

    ctx.progress(f"downloading {chosen.name}")
    got = store.install(resolved, chosen, shown, fetch, root, max_bytes=cap)
    ctx.done(f"{'updated' if got.replaced else 'installed'} {resolved.title} {resolved.version} ({got.verified}) -> {got.path}")
    ctx.emit({**plan, "path": got.path, "files": len(got.files), "verified": got.verified})

    return 0


def _find(root: str, query: str) -> dict[str, Any]:
    data = store.load_manifest(root)
    hit = store.find(data, query)

    if not hit:
        raise CliError(f"no installed mod matches {query!r}", hint="Run 'tmc mods list'.")

    return hit


def remove(ctx: Context) -> int:
    root = store.store_root(ctx.args.dest)
    entry = _find(root, ctx.args.mod)

    if ctx.dry_run:
        ctx.emit({"id": entry["id"], "path": entry["path"], "dryRun": True})

        return 0

    if not ctx.confirm(f"Remove {entry['title']} ({entry['path']})?"):
        return 1

    store.remove(root, entry)
    ctx.done(f"removed {entry['title']}")

    return 0


def list_mods(ctx: Context) -> int:
    root = store.store_root(ctx.args.dest)
    rows = [
        {"id": m["id"], "title": m["title"], "version": m["version"], "verified": m["verified"], "path": m["path"]}
        for m in store.load_manifest(root)["mods"]
    ]
    ctx.emit(rows, columns=["id", "title", "version", "verified", "path"])

    return 0


def link_status(ctx: Context) -> int:
    ctx.emit(links.all_status(), columns=["scheme", "state", "by", "ours"])

    return 0
