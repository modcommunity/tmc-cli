"""The CLI contract: what the SITE says its types and commands are.

WHY
---
`schema.py` is a hand-written mirror of the server's content registry, and its
own docstring is honest about the failure mode: nothing breaks when the site adds
a field, the field just looks like a typo, `tmc schema` omits it, and completion
never offers it. The mirror rots quietly, and `scripts/schema-drift.py` only
finds out if somebody runs it against a checkout of the website next door.

The site now publishes what it knows:

    GET <base>/api/content/spec        (no key — the field LIST is not a value)

That document carries every content type with its create/update fields, the
relations each one has, and the command grammar the website's own console
dispatches. See `../../website-city/docs/api/cli-contract.md`.

WHAT THIS MODULE DOES WITH IT
-----------------------------
Fetches it, caches it under the config directory, and OVERLAYS it on `TYPES`.
The commands stay exactly as `cli.py` declares them — argparse builds the parser
at import time, `--help` has to work with no network, and a CLI whose command
list depends on the last successful fetch is a CLI whose `--help` differs between
two machines. So:

  * **Field existence, types and requiredness: the contract wins.** It is the
    live server; the mirror is a guess about it.
  * **Notes and enums: the mirror wins.** The contract carries neither, and
    losing "slug; changing it leaves a redirect behind" to gain nothing would
    make `tmc schema` worse.
  * **Commands: the mirror wins, and `tmc contract drift` reports the gap.**
    Anything the site grew is reachable through `tmc raw` today and through a
    real command after the next release.

PROFILE SAFETY
--------------
A cached contract records the base url it came from and is applied only against
that same site. Two profiles pointed at a production site and a dev checkout have
different registries, and silently judging one by the other's field list is worse
than having no contract at all.

STALENESS
---------
A contract older than `MAX_AGE_DAYS` is ignored rather than trusted. A mirror
that is a month behind is a known quantity; a cached answer from a site that has
since been redeployed twice only looks current.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import replace
from typing import Any

from .config import config_dir, open_private
from .errors import CliError
from . import schema as schema_mod
from .schema import (
    BOOL,
    DATE,
    FLOAT,
    INT,
    INT_LIST,
    JSON,
    STR,
    STR_LIST,
    TYPES,
    Field,
    TypeSpec,
)

#: The wire shape this build understands. The document carries its own; a higher
#: one is read for what we recognise, a lower one is refused.
SUPPORTED_CONTRACT = 1

#: Past this, a cached contract is treated as absent.
MAX_AGE_DAYS = 30

#: The site's field-type names → this CLI's. The two vocabularies were written
#: independently; this is the only place they have to agree.
TYPE_MAP: dict[str, str] = {
    "string": STR,
    "int": INT,
    "float": FLOAT,
    "bool": BOOL,
    "date": DATE,
    "json": JSON,
    "strList": STR_LIST,
    "intList": INT_LIST,
    # The site could not read the field's schema. Treat it as a string: the
    # server judges it either way, and refusing to send it would be worse.
    "unknown": STR,
}


def contract_path() -> str:
    return os.path.join(config_dir(), "contract.json")


def spec_url(base_url: str) -> str:
    """Where the contract lives, for either shape of base url.

    `api.moddingcommunity.com` serves the content API at `/content`, while the
    website's own origin needs the `/api` prefix — the same split every other
    path in this tool deals with. Both are accepted, so a profile pointed at
    either works without the caller knowing which they have.
    """

    base = base_url.rstrip("/")

    if base.endswith("/api"):
        return f"{base}/content/spec"

    return f"{base}/api/content/spec"


# ---- fetching ----------------------------------------------------------------


def fetch(base_url: str, timeout: float = 15.0, verify_tls: bool = True) -> dict[str, Any]:
    """Download the contract. No credential — this endpoint takes none."""

    url = spec_url(base_url)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})

    ctx = None

    if not verify_tls:
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read()
    except urllib.error.HTTPError as err:
        if err.code == 404:
            raise CliError(
                f"{url} answered 404.",
                hint=(
                    "That site is older than the contract endpoint. The built-in "
                    "mirror still works — this only means it cannot be checked."
                ),
            ) from None

        raise CliError(f"{url} answered {err.code}.") from None
    except urllib.error.URLError as err:
        raise CliError(f"Could not reach {url}: {err.reason}") from None

    try:
        doc = json.loads(body)
    except ValueError:
        raise CliError(f"{url} did not answer JSON.") from None

    if not isinstance(doc, dict):
        raise CliError(f"{url} answered JSON, but not an object.")

    version = doc.get("contract")

    if not isinstance(version, int):
        raise CliError("That document has no 'contract' version — it is not a contract.")

    if version > SUPPORTED_CONTRACT:
        # Forward compatible ON PURPOSE. A newer contract that added keys is
        # still readable for everything this build knows about, and refusing it
        # would make a site deploy break every CLI that had not been updated yet.
        pass

    if version < SUPPORTED_CONTRACT:
        raise CliError(
            f"That site serves contract {version}; this CLI needs {SUPPORTED_CONTRACT}.",
            hint="Use an older tmc-cli against that site, or upgrade the site.",
        )

    doc["_baseUrl"] = base_url.rstrip("/")
    doc["_fetchedAt"] = int(time.time())

    return doc


def save(doc: dict[str, Any]) -> str:
    """Write the cache. Private like everything else this tool writes.

    The contract holds no secret, but it sits in the same directory as the
    credentials and inherits the same habit — one rule about that directory is
    easier to keep than two.
    """

    path = contract_path()
    os.makedirs(config_dir(), mode=0o700, exist_ok=True)

    tmp = f"{path}.tmp"

    with open_private(tmp) as handle:
        json.dump(doc, handle, indent=2)
        handle.write("\n")

    os.replace(tmp, path)

    return path


def load() -> dict[str, Any] | None:
    """The cached contract, or None. Never raises — a corrupt cache is absent."""

    try:
        with open(contract_path(), encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        return None

    if not isinstance(doc, dict) or "types" not in doc:
        return None

    return doc


def clear() -> bool:
    try:
        os.remove(contract_path())
        return True
    except OSError:
        return False


def age_days(doc: dict[str, Any]) -> float | None:
    fetched = doc.get("_fetchedAt")

    if not isinstance(fetched, int):
        return None

    return (time.time() - fetched) / 86400.0


def usable_for(doc: dict[str, Any] | None, base_url: str) -> bool:
    """Whether this cached contract may judge this site."""

    if not doc:
        return False

    if doc.get("_baseUrl", "").rstrip("/") != base_url.rstrip("/"):
        return False

    age = age_days(doc)

    return age is None or age <= MAX_AGE_DAYS


# ---- overlaying --------------------------------------------------------------


def _fields_from(entries: Any, known: dict[str, Field]) -> tuple[Field, ...]:
    """Contract fields, keeping whatever the mirror knows that it does not."""

    out: list[Field] = []

    for entry in entries or ():
        if not isinstance(entry, dict):
            continue

        name = entry.get("name")

        if not isinstance(name, str):
            continue

        mapped = TYPE_MAP.get(str(entry.get("type")), STR)
        local = known.get(name)

        if local is not None:
            # The mirror's note, enum and staff-only flag survive; the TYPE comes
            # from the server, which is the thing that will reject it.
            out.append(replace(local, type=mapped))
        else:
            out.append(Field(name, mapped))

    return tuple(out)


def merged_types(doc: dict[str, Any]) -> dict[str, TypeSpec]:
    """`TYPES`, corrected by the contract."""

    out: dict[str, TypeSpec] = {}

    for entry in doc.get("types") or ():
        if not isinstance(entry, dict):
            continue

        name = entry.get("name")

        if not isinstance(name, str):
            continue

        local = TYPES.get(name)
        known = {item.name: item for item in (local.fields if local else ())}

        create = entry.get("create") or []

        required = tuple(
            item["name"]
            for item in create
            if isinstance(item, dict) and item.get("required") and isinstance(item.get("name"), str)
        )

        relations = tuple(
            r for r in (entry.get("relations") or ()) if isinstance(r, str)
        )

        if local is None:
            # A type this build has never heard of. It still gets fields and
            # relations; what it cannot get is a sub-parser, because those were
            # built at import time — `tmc raw` is the way to reach it until the
            # next release.
            out[name] = TypeSpec(
                name=name,
                canonical=bool(entry.get("canonical")),
                required=required,
                fields=_fields_from(create, {}),
                relations=relations,
                staff_only_write=bool(entry.get("staffOnlyWrite")),
                note="not in this CLI build — reachable through 'tmc raw'",
            )

            continue

        out[name] = replace(
            local,
            canonical=bool(entry.get("canonical")),
            required=required,
            fields=_fields_from(create, known),
            relations=relations,
            staff_only_write=bool(entry.get("staffOnlyWrite")),
        )

    # A type the mirror has and the contract does not is GONE from that site.
    # Kept, so `tmc schema` can still describe it, but that is a drift finding.
    for name, local in TYPES.items():
        out.setdefault(name, local)

    return out


def install(doc: dict[str, Any]) -> None:
    """Point the schema lookups at the contract's view of the types."""

    schema_mod.install_override(merged_types(doc))


def install_if_usable(base_url: str) -> dict[str, Any] | None:
    """Apply the cached contract when it is for this site and fresh enough."""

    doc = load()

    if not usable_for(doc, base_url):
        return None

    assert doc is not None
    install(doc)

    return doc


# ---- drift -------------------------------------------------------------------


def remote_commands(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """The site's commands, as entries rather than paths.

    Entries, not a flat set, because a command has ALIASES and a `local` flag and
    both matter to the comparison. Flattening first is what made the first
    version of this report claim the CLI was missing `mod edit` — which is the
    same command as `mod update`, spelled the way a lot of fingers spell it.
    """

    out: list[dict[str, Any]] = []

    for entry in doc.get("commands") or ():
        if not isinstance(entry, dict):
            continue

        path = entry.get("path")

        if isinstance(path, list) and all(isinstance(p, str) for p in path):
            out.append(entry)

    return out


def command_names(doc: dict[str, Any]) -> set[str]:
    """Canonical command names, for saying what one sync changed."""

    return {" ".join(entry["path"]) for entry in remote_commands(doc)}


def spellings(entry: dict[str, Any]) -> set[tuple[str, ...]]:
    """Every way one command may be typed: its path, plus each alias."""

    path = tuple(entry["path"])
    out = {path}

    for alias in entry.get("aliases") or ():
        if isinstance(alias, str) and path:
            out.add(path[:-1] + (alias,))

    return out


#: Commands that are this tool's alone, and always will be.
#:
#: A browser has no shell to complete into, no local file to read a relation
#: template into, and nothing to download a file TO. Reporting them forever as
#: "the site's console does not have this" would train everybody to ignore the
#: report, which is the one thing a drift check cannot survive.
CLI_ONLY: set[tuple[str, ...]] = {
    ("completion",),
    ("template",),
    ("file", "download"),
    ("contract", "sync"),
    ("contract", "show"),
    ("contract", "drift"),
    ("contract", "clear"),
}


def diff(doc: dict[str, Any], local_commands: set[tuple[str, ...]]) -> dict[str, Any]:
    """What the site has that this build does not, and the other way round.

    Returns a report rather than printing one, so the same comparison serves the
    command, the completion hint and the CI script.
    """

    remote_types = {
        entry["name"]: entry
        for entry in (doc.get("types") or ())
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }

    types_added = sorted(set(remote_types) - set(TYPES))
    types_removed = sorted(set(TYPES) - set(remote_types))

    fields_added: dict[str, list[str]] = {}
    fields_removed: dict[str, list[str]] = {}
    fields_retyped: dict[str, list[str]] = {}
    fields_narrowed: dict[str, list[str]] = {}
    relations_changed: dict[str, dict[str, list[str]]] = {}

    for name, entry in remote_types.items():
        local = TYPES.get(name)

        if local is None:
            continue

        local_fields = {item.name: item.type for item in local.fields}
        remote_fields = {
            item["name"]: TYPE_MAP.get(str(item.get("type")), STR)
            for item in (entry.get("create") or ())
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }

        added = sorted(set(remote_fields) - set(local_fields))
        removed = sorted(set(local_fields) - set(remote_fields))

        retyped: list[str] = []
        narrowed: list[str] = []

        for field_name in sorted(set(local_fields) & set(remote_fields)):
            mine = local_fields[field_name]
            theirs = remote_fields[field_name]

            if mine == theirs:
                continue

            line = f"{field_name} ({mine} → {theirs})"

            # This CLI being STRICTER than the server is a choice, not drift.
            # Most id columns are declared `z.number()` with no `.int()`, so the
            # server would take `3.5` for an `appId`; refusing it here costs a
            # caller nothing and catches a typo. Reported, but it does not fail
            # the check — otherwise the check fails forever and stops being read.
            if mine == INT and theirs == FLOAT:
                narrowed.append(line)
            else:
                retyped.append(line)

        if added:
            fields_added[name] = added

        if removed:
            fields_removed[name] = removed

        if retyped:
            fields_retyped[name] = retyped

        if narrowed:
            fields_narrowed[name] = narrowed

        local_rel = set(local.relations)
        remote_rel = {r for r in (entry.get("relations") or ()) if isinstance(r, str)}

        if local_rel != remote_rel:
            relations_changed[name] = {
                "added": sorted(remote_rel - local_rel),
                "removed": sorted(local_rel - remote_rel),
            }

    # ---- commands ----------------------------------------------------------
    #
    # A remote command counts as present when the CLI has ANY of its spellings;
    # a local command counts as present when it is any spelling of a remote one.

    entries = remote_commands(doc)

    covered: set[tuple[str, ...]] = set()
    commands_added: list[str] = []

    for entry in entries:
        ways = spellings(entry)
        here = ways & local_commands

        covered |= here

        if here or entry.get("local"):
            continue

        commands_added.append(" ".join(entry["path"]))

    commands_removed = sorted(
        " ".join(path)
        for path in local_commands - covered
        if path not in CLI_ONLY
    )

    return {
        "types": {"added": types_added, "removed": types_removed},
        "fields": {
            "added": fields_added,
            "removed": fields_removed,
            "retyped": fields_retyped,
            "narrowed": fields_narrowed,
        },
        "relations": relations_changed,
        "commands": {
            "added": sorted(commands_added),
            "removed": commands_removed,
        },
    }


def is_clean(report: dict[str, Any]) -> bool:
    return not any(
        [
            report["types"]["added"],
            report["types"]["removed"],
            report["fields"]["added"],
            report["fields"]["removed"],
            report["fields"]["retyped"],
            report["relations"],
            report["commands"]["added"],
            report["commands"]["removed"],
        ]
    )
