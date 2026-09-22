"""`tmc contract` — keep this CLI and the site agreeing about what exists.

The site publishes its content registry and the command grammar of its own web
console at `<base>/api/content/spec`. This is the half of that which lives here:
fetch it, cache it, and say what it disagrees with.

WHAT SYNC CHANGES AND WHAT IT DOES NOT
--------------------------------------
After a sync, `--set` coercion, the unknown-field check, `tmc schema` and the
completion scripts all answer from the SITE's field list. The commands do not
change — see the note in `contract.py` for why a CLI whose `--help` depends on
the last successful fetch is a worse CLI.

So `sync` makes this tool accurate about fields, and `drift` tells you when the
tool itself needs a release.
"""

from __future__ import annotations

import json
from typing import Any

from .. import contract as contract_mod
from .. import output
from ..config import resolve_unkeyed
from ..context import Context
from ..errors import UsageError

EXIT_DRIFT = 3


def _doc_for(ctx: Context) -> dict[str, Any]:
    """The cached contract, refusing clearly when there is not one to use."""

    doc = contract_mod.load()

    if doc is None:
        raise UsageError(
            "No contract has been fetched yet.",
            hint="Run 'tmc contract sync' first.",
        )

    base = resolve_unkeyed(ctx.args).base_url

    if not contract_mod.usable_for(doc, base):
        cached = doc.get("_baseUrl", "?")
        age = contract_mod.age_days(doc)

        if cached.rstrip("/") != base.rstrip("/"):
            raise UsageError(
                f"The cached contract is from {cached}, and this profile points at {base}.",
                hint="Run 'tmc contract sync' to fetch this site's.",
            )

        raise UsageError(
            f"The cached contract is {age:.0f} days old.",
            hint="Run 'tmc contract sync' to refresh it.",
        )

    return doc


def sync(ctx: Context) -> int:
    # `resolve_unkeyed`, not `ctx.settings`: the contract endpoint takes no
    # credential, and this is the command a fresh install runs BEFORE
    # `tmc auth login`. Demanding a key here would make the tool that fixes the
    # mirror unreachable until the mirror had already been used.
    settings = resolve_unkeyed(ctx.args)
    base = settings.base_url

    ctx.progress(f"fetching {contract_mod.spec_url(base)}")

    doc = contract_mod.fetch(
        base,
        timeout=settings.timeout,
        verify_tls=settings.verify_tls,
    )

    previous = contract_mod.load()
    path = contract_mod.save(doc)

    if ctx.args.output == "json":
        print(json.dumps({"path": path, "contract": doc.get("contract"), "console": doc.get("console")}, indent=2))

        return 0

    types = doc.get("types") or []
    commands = doc.get("commands") or []

    output.info(
        f"contract {doc.get('contract')} (console {doc.get('console')}) from {base}"
    )
    output.info(f"{len(types)} types, {len(commands)} commands → {path}")

    # What MOVED since the last sync, when there was one for this site. A sync
    # that prints only totals cannot tell "nothing changed" from "everything
    # did", which is the whole question somebody runs this to answer.
    if previous and contract_mod.usable_for(previous, base):
        before = {
            entry.get("name"): entry
            for entry in (previous.get("types") or ())
            if isinstance(entry, dict)
        }
        after = {
            entry.get("name"): entry
            for entry in types
            if isinstance(entry, dict)
        }

        gained = sorted(set(after) - set(before))
        lost = sorted(set(before) - set(after))

        moved = [
            name
            for name in sorted(set(before) & set(after))
            if before[name] != after[name]
        ]

        if gained:
            output.info(f"new types: {', '.join(gained)}")

        if lost:
            output.info(f"gone: {', '.join(lost)}")

        if moved:
            output.info(f"changed: {', '.join(moved)}")

        # Commands too, not just types. A sync that reported "nothing changed"
        # while the command count moved by five was answering a question nobody
        # asked.
        before_cmds = contract_mod.command_names(previous)
        after_cmds = contract_mod.command_names(doc)

        cmds_gained = sorted(after_cmds - before_cmds)
        cmds_lost = sorted(before_cmds - after_cmds)

        if cmds_gained:
            output.info(f"new commands: {', '.join(cmds_gained)}")

        if cmds_lost:
            output.info(f"commands gone: {', '.join(cmds_lost)}")

        if not (gained or lost or moved or cmds_gained or cmds_lost):
            output.info("nothing changed since the last sync")

    return 0


def show(ctx: Context) -> int:
    doc = contract_mod.load()

    if doc is None:
        raise UsageError(
            "No contract has been fetched yet.",
            hint="Run 'tmc contract sync' first.",
        )

    if ctx.args.output == "json":
        print(json.dumps(doc, indent=2))

        return 0

    age = contract_mod.age_days(doc)

    output.info(f"source     {doc.get('_baseUrl', '?')}")
    output.info(f"contract   {doc.get('contract')}")
    output.info(f"console    {doc.get('console')}")
    output.info(f"built      {doc.get('generatedAt', '?')}")
    output.info(f"fetched    {age:.1f} days ago" if age is not None else "fetched    ?")
    output.info(f"types      {len(doc.get('types') or [])}")
    output.info(f"commands   {len(doc.get('commands') or [])}")

    if not contract_mod.usable_for(doc, resolve_unkeyed(ctx.args).base_url):
        output.error("not in use: it is for another site, or too old to trust")

    return 0


def clear(ctx: Context) -> int:
    removed = contract_mod.clear()

    output.info("contract cache cleared" if removed else "nothing cached")

    return 0


def drift(ctx: Context) -> int:
    """Compare the site's contract with what this build believes.

    Exits `EXIT_DRIFT` when they disagree, so CI can run it. `--fetch` makes it a
    one-shot check against a live site without touching the cache, which is the
    form a pipeline wants.
    """

    if getattr(ctx.args, "fetch", False):
        settings = resolve_unkeyed(ctx.args)

        doc = contract_mod.fetch(
            settings.base_url,
            timeout=settings.timeout,
            verify_tls=settings.verify_tls,
        )
    else:
        doc = _doc_for(ctx)

    # Imported here rather than at module scope: `cli` imports the command
    # modules to build the parser, so importing it back at the top would be a
    # cycle.
    from ..cli import local_command_paths

    report = contract_mod.diff(doc, local_command_paths())

    if ctx.args.output == "json":
        print(json.dumps(report, indent=2))

        return 0 if contract_mod.is_clean(report) else EXIT_DRIFT

    if contract_mod.is_clean(report):
        print(f"in step with {doc.get('_baseUrl', resolve_unkeyed(ctx.args).base_url)}")

        return 0

    def block(title: str, lines: list[str]) -> None:
        if not lines:
            return

        print(title)

        for line in lines:
            print(f"  {line}")

    block(
        "types the site has that this CLI does not:",
        report["types"]["added"],
    )
    block(
        "types this CLI has that the site does not:",
        report["types"]["removed"],
    )

    block(
        "fields the site has that this CLI does not:",
        [f"{t}: {', '.join(f)}" for t, f in sorted(report["fields"]["added"].items())],
    )
    block(
        "fields this CLI has that the site does not:",
        [f"{t}: {', '.join(f)}" for t, f in sorted(report["fields"]["removed"].items())],
    )
    block(
        "fields whose type changed:",
        [f"{t}: {', '.join(f)}" for t, f in sorted(report["fields"]["retyped"].items())],
    )

    # Informational: this CLI is stricter than the server on these, which is a
    # choice rather than a disagreement. Printed so it is visible, and left out
    # of the exit code so the check stays worth running.
    block(
        "fields this CLI is stricter about (not drift):",
        [f"{t}: {', '.join(f)}" for t, f in sorted(report["fields"]["narrowed"].items())],
    )

    block(
        "relations that moved:",
        [
            f"{t}: +{', '.join(d['added']) or '-'} / -{', '.join(d['removed']) or '-'}"
            for t, d in sorted(report["relations"].items())
        ],
    )

    block(
        "commands the site has that this CLI does not:",
        [f"tmc {c}" for c in report["commands"]["added"]],
    )
    block(
        "commands this CLI has that the site's console does not:",
        [f"tmc {c}" for c in report["commands"]["removed"]],
    )

    print()
    print(
        "Fields are already corrected by a synced contract; commands need a "
        "release of this CLI. 'tmc raw' reaches anything in the meantime."
    )

    return EXIT_DRIFT
