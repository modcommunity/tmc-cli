"""Provider adapters: a `Ref` to a `Resolved` (files to fetch). Mirrors `providers/*.rs`.

The network is behind `Fetch`, so every mapping below is a pure function the
tests feed canned JSON, and nothing in the test run opens a socket.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..errors import CliError, NetworkError
from ..version import USER_AGENT
from .parse import DIRECT_EXTENSIONS, Ref


@dataclass
class RemoteFile:
    url: str
    name: str
    size: int | None = None
    hashes: dict[str, str] = field(default_factory=dict)
    primary: bool = False


@dataclass
class Resolved:
    provider: str
    project_id: str
    version_id: str
    title: str
    version: str = ""
    author: str | None = None
    files: list[RemoteFile] = field(default_factory=list)
    deps: list[str] = field(default_factory=list)
    page_url: str | None = None
    manual_only: str | None = None  # the page to open when third-party download is off


class Fetch(Protocol):
    def get_json(self, url: str) -> Any: ...

    def download(self, url: str, dest: str, *, max_bytes: int, progress: Callable[[int, int | None], None] | None) -> int: ...


class ProviderError(CliError):
    pass


class HttpFetch:
    """urllib, https only, with the identifying User-Agent Modrinth requires."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def _open(self, url: str):
        if not url.lower().startswith("https://"):
            raise ProviderError(f"refusing a non-https link: {url.split('?')[0]}")

        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, */*"})

        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as err:
            raise ProviderError(f"{urllib.parse.urlsplit(url).netloc} answered {err.code} {err.reason}") from None
        except (urllib.error.URLError, OSError) as err:
            raise NetworkError(f"could not reach {urllib.parse.urlsplit(url).netloc}: {getattr(err, 'reason', err)}") from None

        # A redirect must not downgrade to plain http.
        if not resp.geturl().lower().startswith("https://"):
            resp.close()
            raise ProviderError("the link redirected to a plain http address; refused")

        return resp

    def get_json(self, url: str) -> Any:
        with self._open(url) as resp:
            try:
                return json.loads(resp.read().decode("utf-8"))
            except ValueError:
                raise ProviderError("the provider sent something that is not JSON") from None

    def download(self, url: str, dest: str, *, max_bytes: int, progress: Callable[[int, int | None], None] | None) -> int:
        total = 0

        with self._open(url) as resp, open(dest, "wb") as out:
            declared = resp.headers.get("Content-Length")
            expect = int(declared) if declared and declared.isdigit() else None

            if max_bytes and expect and expect > max_bytes:
                raise ProviderError(_over_cap(expect, max_bytes))

            while True:
                chunk = resp.read(256 * 1024)

                if not chunk:
                    break

                total += len(chunk)

                if max_bytes and total > max_bytes:
                    raise ProviderError(_over_cap(total, max_bytes))

                out.write(chunk)

                if progress:
                    progress(total, expect)

        return total


def _over_cap(size: int, cap: int) -> str:
    return (
        f"the download is over {size // (1024 * 1024)} MB, past the {cap // (1024 * 1024)} MB limit "
        "(--max-mb; 0 means no limit)"
    )


# -- pure mappings ------------------------------------------------------------------


def _s(obj: Any, key: str) -> str | None:
    v = obj.get(key) if isinstance(obj, dict) else None

    return v if isinstance(v, str) and v else None


def _enc(v: str) -> str:
    return urllib.parse.quote_plus(v)


def map_modrinth(project: dict[str, Any], version: dict[str, Any]) -> Resolved:
    files = []

    for f in version.get("files") or []:
        url, name = _s(f, "url"), _s(f, "filename")

        if url and name:
            hashes = {k: v for k, v in (f.get("hashes") or {}).items() if k in ("sha1", "sha512") and isinstance(v, str)}
            files.append(RemoteFile(url, name, f.get("size") if isinstance(f.get("size"), int) else None, hashes, bool(f.get("primary"))))

    if not files:
        raise ProviderError("that Modrinth version has no files to download")

    if not _s(version, "id"):
        raise ProviderError("Modrinth sent a version with no id")

    slug = _s(project, "slug") or _s(project, "id") or ""
    deps = [d["project_id"] for d in version.get("dependencies") or [] if d.get("dependency_type") == "required" and d.get("project_id")]

    return Resolved(
        "modrinth", _s(project, "id") or _s(version, "project_id") or slug, version["id"],
        _s(project, "title") or slug, _s(version, "version_number") or "", None, files, deps,
        f"https://modrinth.com/{_s(project, 'project_type') or 'mod'}/{slug}",
    )


def map_thunderstore(project: str, body: dict[str, Any]) -> Resolved:
    v = body.get("latest") or body
    url = _s(v, "download_url")

    if not url:
        raise ProviderError("Thunderstore sent no download link")

    version = _s(v, "version_number") or ""
    name = _s(v, "full_name") or project.replace("/", "-")
    deps = []

    for d in v.get("dependencies") or []:
        # "Owner-Name-1.2.3"
        rest, _, _ver = str(d).rpartition("-")
        owner, _, pkg = rest.partition("-")

        if owner and pkg:
            deps.append(f"{owner}/{pkg}")

    size = v.get("file_size")

    return Resolved(
        "thunderstore", project, version, _s(body, "name") or _s(v, "name") or project, version,
        _s(body, "owner") or _s(v, "namespace"),
        [RemoteFile(url, f"{name}-{version}.zip", size if isinstance(size, int) else None, {}, True)],
        deps, _s(body, "package_url"),
    )


def _installable(name: str) -> bool:
    low = name.lower()

    return any(low.endswith("." + e) for e in DIRECT_EXTENSIONS)


def map_github(repo: str, rel: dict[str, Any], want: str | None) -> Resolved:
    tag = _s(rel, "tag_name")

    if not tag:
        raise ProviderError("GitHub sent a release with no tag")

    files = []

    for a in rel.get("assets") or []:
        name, url = _s(a, "name"), _s(a, "browser_download_url")

        if not name or not url or (want and want != name) or (not want and not _installable(name)):
            continue

        digest = _s(a, "digest") or ""
        hashes = {"sha256": digest[len("sha256:"):]} if digest.startswith("sha256:") else {}
        files.append(RemoteFile(url, name, a.get("size") if isinstance(a.get("size"), int) else None, hashes))

    if not files:
        raise ProviderError(f"release {tag} of {repo} has no .zip, .jar or similar file to install")

    files[0].primary = True

    return Resolved(
        "github", repo, tag, repo.rsplit("/", 1)[-1], tag, (rel.get("author") or {}).get("login"),
        files, [], _s(rel, "html_url"),
    )


def resolve_direct(ref: Ref) -> Resolved:
    u = urllib.parse.urlsplit(ref.url)
    name = ref.extras.get("file") or (u.path.rstrip("/").rsplit("/", 1)[-1])

    if not name:
        raise ProviderError("the link names no file")

    frag = u.fragment.lower()
    hashes = {"sha256": frag[len("sha256="):]} if frag.startswith("sha256=") else {}
    bare = urllib.parse.urlunsplit(u._replace(fragment=""))
    title = name.rsplit(".", 1)[0] if "." in name else name

    return Resolved("direct", bare, bare, title, "", u.hostname, [RemoteFile(bare, name, None, hashes, True)])


def resolve_steam(ref: Ref) -> Resolved:
    page = f"https://steamcommunity.com/sharedfiles/filedetails/?id={ref.project}"

    return Resolved("steam", ref.project or "", ref.project or "", "Steam Workshop item", page_url=page, manual_only=page)


def map_site(provider: str, body: dict[str, Any]) -> Resolved:
    """The site's already-mapped answer (`Resolved`, camelCase) with the app's checks."""

    if not isinstance(body, dict) or body.get("provider") != provider:
        raise ProviderError("the site answered for a different provider than the one asked")

    files = []

    for f in body.get("files") or []:
        url = _s(f, "url")

        if not url or not url.lower().startswith("https://"):
            raise ProviderError("the site sent a file link that is not https; refused")

        files.append(RemoteFile(url, _s(f, "name") or "file", f.get("size") if isinstance(f.get("size"), int) else None,
                                {k: v for k, v in (f.get("hashes") or {}).items() if isinstance(v, str)}, bool(f.get("primary"))))

    manual = _s(body, "manualOnly")

    if not files and not manual:
        raise ProviderError("the site sent no file to download")

    return Resolved(
        provider, str(body.get("projectId") or ""), str(body.get("versionId") or ""),
        _s(body, "title") or "", _s(body, "version") or "", _s(body, "author"), files,
        [str(d.get("project")) for d in body.get("deps") or [] if isinstance(d, dict) and d.get("project")],
        _s(body, "pageUrl"), manual,
    )


# -- the free providers -----------------------------------------------------------


def resolve_free(ref: Ref, fetch: Fetch) -> Resolved:
    """Modrinth, Thunderstore, GitHub, direct, Steam: nothing here needs a key."""

    if ref.provider == "direct":
        return resolve_direct(ref)

    if ref.provider == "steam":
        return resolve_steam(ref)

    if not ref.project:
        raise ProviderError(f"the {ref.provider} link names no project")

    if ref.provider == "modrinth":
        base = "https://api.modrinth.com/v2"
        project = fetch.get_json(f"{base}/project/{_enc(ref.project)}")

        if ref.version:
            version = fetch.get_json(f"{base}/version/{_enc(ref.version)}")
        else:
            versions = fetch.get_json(f"{base}/project/{_enc(ref.project)}/version")

            if not versions:
                raise ProviderError("that Modrinth project has no versions")

            version = versions[0]

        return map_modrinth(project, version)

    if ref.provider == "thunderstore":
        tail = f"{ref.version}/" if ref.version else ""
        body = fetch.get_json(f"https://thunderstore.io/api/experimental/package/{ref.project}/{tail}")

        return map_thunderstore(ref.project, body)

    if ref.provider == "github":
        tag = f"tags/{ref.version}" if ref.version else "latest"
        rel = fetch.get_json(f"https://api.github.com/repos/{ref.project}/releases/{tag}")

        return map_github(ref.project, rel, ref.extras.get("asset"))

    raise ProviderError(f"no adapter for {ref.provider}")


def env_max_mb() -> int | None:
    raw = os.environ.get("TMC_MODS_MAX_MB")

    return int(raw) if raw and raw.isdigit() else None
