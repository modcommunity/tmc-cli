"""Pasted text to a typed `Ref`. Pure: no network, no disk. Mirrors `parse.rs`."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

from ..errors import UsageError

#: `DIRECT_EXTENSIONS` in parse.rs.
DIRECT_EXTENSIONS = ("zip", "jar", "7z", "rar", "tar.gz", "mrpack", "gma", "cs", "dll")

#: Schemes the OS hands the app; `EXTERNAL_SCHEMES` in parse.rs.
SCHEMES = ("nxm", "curseforge", "modrinth", "ror2mm", "prismlauncher", "steam")

#: Providers that need a key the site (or the user) holds.
KEYED = ("curseforge", "nexus", "modio")


@dataclass
class Ref:
    provider: str
    url: str
    project: str | None = None
    version: str | None = None
    game: str | None = None
    kind: str | None = None  # the link's class: mod, plugin, mc-mods...
    extras: dict[str, str] = field(default_factory=dict)


class ParseError(UsageError):
    pass


def _first_candidate(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")

        if not line or line.startswith("#") or line.startswith("["):
            continue

        for prefix in ("URL=", "url="):
            if line.startswith(prefix):
                line = line[len(prefix):]
                break

        line = line.strip().strip("\"'<>")

        if line:
            return line

    return None


def _segs(path: str) -> list[str]:
    return [p for p in path.split("/") if p]


def _query(query: str, key: str) -> str | None:
    for k, v in parse_qsl(query, keep_blank_values=True):
        if k.lower() == key.lower() and v:
            return v

    return None


def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _has_direct_ext(path: str) -> bool:
    low = path.lower()

    return any(low.endswith("." + e) for e in DIRECT_EXTENSIONS)


def parse_input(text: str) -> Ref:
    raw = _first_candidate(text)

    if raw is None:
        raise ParseError("nothing to add: the text is empty")

    try:
        parts = urlsplit(raw)
    except ValueError:
        raise ParseError("that is not a link") from None

    if not parts.scheme or "://" not in raw:
        raise ParseError("that is not a link")

    scheme = parts.scheme.lower()

    if scheme == "http":
        raise ParseError("plain http links are refused; use the https version of this link")

    if scheme == "https":
        return _web(raw, parts)

    handlers = {
        "modrinth": _modrinth_scheme,
        "ror2mm": _ror2mm,
        "curseforge": _curseforge_scheme,
        "nxm": _nxm,
        "prismlauncher": _prism,
        "steam": _steam_scheme,
    }

    if scheme in handlers:
        return handlers[scheme](raw, parts)

    raise ParseError(f"unsupported link: the {scheme}:// scheme")


def _incomplete(what: str, why: str) -> ParseError:
    return ParseError(f"the {what} link is incomplete: {why}")


def _web(raw: str, u) -> Ref:  # noqa: C901 - one branch per site, like parse.rs
    s = _segs(u.path)
    host = (u.hostname or "").lower()

    if _host_is(host, "modrinth.com"):
        if len(s) >= 2 and s[0] in ("mod", "plugin", "datapack", "resourcepack", "shader", "modpack"):
            r = Ref("modrinth", raw, project=s[1], kind=s[0])

            if len(s) >= 4 and s[2] == "version":
                r.version = s[3]

            return r

        raise _incomplete("Modrinth", "expected modrinth.com/mod/<name>")

    if _host_is(host, "thunderstore.io"):
        game = None
        rest: list[str] = []

        if s and s[0] == "c" and "p" in s and len(s) > s.index("p") + 2:
            i = s.index("p")
            game, rest = s[1] if len(s) > 1 else None, s[i + 1:]
        elif s and s[0] == "package":
            rest = s[1:]

        if len(rest) >= 2 and rest[0] != "download":
            r = Ref("thunderstore", raw, project=f"{rest[0]}/{rest[1]}", game=game)
            r.version = rest[2] if len(rest) > 2 else None

            return r

        if len(s) >= 5 and s[0] == "package" and s[1] == "download":
            return Ref("thunderstore", raw, project=f"{s[2]}/{s[3]}", version=s[4])

        raise _incomplete("Thunderstore", "expected thunderstore.io/package/<owner>/<name>")

    if _host_is(host, "github.com") and len(s) >= 2:
        if len(s) >= 6 and s[2] == "releases" and s[3] == "download":
            return Ref("github", raw, project=f"{s[0]}/{s[1]}", version=s[4], extras={"asset": s[5]})

        name = s[1][:-4] if s[1].endswith(".git") else s[1]
        r = Ref("github", raw, project=f"{s[0]}/{name}")

        if len(s) >= 5 and s[2] == "releases" and s[3] == "tag":
            r.version = s[4]

        return r

    if _host_is(host, "curseforge.com"):
        if len(s) >= 3:
            r = Ref("curseforge", raw, game=s[0], kind=s[1], project=s[2])

            if len(s) >= 5 and s[3] == "files":
                r.version = s[4]

            return r

        raise _incomplete("CurseForge", "expected curseforge.com/<game>/<class>/<project>")

    if _host_is(host, "nexusmods.com"):
        if len(s) >= 3 and s[1] == "mods":
            return Ref("nexus", raw, game=s[0], project=s[2], version=_query(u.query, "file_id"))

        raise _incomplete("Nexus", "expected nexusmods.com/<game>/mods/<id>")

    if _host_is(host, "steamcommunity.com"):
        item = _query(u.query, "id")

        if item and item.isdigit():
            return Ref("steam", raw, project=item)

        raise _incomplete("Steam Workshop", "expected ?id=<number>")

    if _host_is(host, "mod.io"):
        if len(s) >= 4 and s[0] == "g" and s[2] == "m":
            return Ref("modio", raw, game=s[1], project=s[3])

        raise _incomplete("mod.io", "expected mod.io/g/<game>/m/<mod>")

    if _has_direct_ext(u.path):
        return Ref("direct", raw, extras={"file": s[-1] if s else ""})

    raise ParseError(
        f"unsupported link: {host or 'that site'} is not a mod site this tool knows, "
        "and the link does not end in a file such as .zip or .jar"
    )


def _host_and_segs(u) -> list[str]:
    # `modrinth://mod/sodium`: the first word is the URL's host.
    return ([u.hostname] if u.hostname else []) + _segs(u.path)


def _modrinth_scheme(raw: str, u) -> Ref:
    parts = _host_and_segs(u)

    if len(parts) >= 2:
        return Ref("modrinth", raw, kind=parts[0], project=parts[1], version=parts[2] if len(parts) > 2 else None)

    raise _incomplete("Modrinth", "expected modrinth://mod/<name>")


def _ror2mm(raw: str, u) -> Ref:
    parts = _host_and_segs(u)

    if "install" not in parts:
        raise _incomplete("ror2mm", "expected .../install/thunderstore.io/<owner>/<name>/<version>")

    rest = parts[parts.index("install") + 1:]

    if rest and "." in rest[0]:
        rest = rest[1:]

    if len(rest) < 2:
        raise _incomplete("ror2mm", "owner and package name are missing")

    return Ref("thunderstore", raw, project=f"{rest[0]}/{rest[1]}", version=rest[2] if len(rest) > 2 else None)


def _curseforge_scheme(raw: str, u) -> Ref:
    addon = _query(u.query, "addonId")

    if not addon:
        raise _incomplete("CurseForge", "addonId is missing")

    return Ref("curseforge", raw, project=addon, version=_query(u.query, "fileId"))


def _nxm(raw: str, u) -> Ref:
    if not u.hostname:
        raise _incomplete("Nexus", "the game is missing")

    s = _segs(u.path)

    if len(s) < 2 or s[0] != "mods":
        raise _incomplete("Nexus", "expected nxm://<game>/mods/<id>/files/<id>")

    r = Ref("nexus", raw, game=u.hostname, project=s[1])

    if len(s) >= 4 and s[2] == "files":
        r.version = s[3]

    for k in ("key", "expires", "user_id"):
        v = _query(u.query, k)

        if v:
            r.extras[k] = v

    return r


def _prism(raw: str, u) -> Ref:
    inner = _query(u.query, "url")

    if inner:
        r = parse_input(inner)
        r.extras["via"] = "prismlauncher"

        return r

    if _query(u.query, "addonId"):
        r = _curseforge_scheme(raw, u)
        r.extras["via"] = "prismlauncher"

        return r

    raise _incomplete("Prism", "expected ?url=<link> or ?addonId=<id>")


def _steam_scheme(raw: str, u) -> Ref:
    for part in reversed(_segs(u.path)):
        if part.isdigit():
            return Ref("steam", raw, project=part)

    raise _incomplete("Steam Workshop", "no item id")
