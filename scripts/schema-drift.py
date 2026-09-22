#!/usr/bin/env python3
"""Compare `schema.py`'s mirror against website-city's real content registry.

PREFER `tmc contract drift`
---------------------------
The site now PUBLISHES its registry and its console's command grammar at
`<base>/api/content/spec`, and `tmc contract sync` / `tmc contract drift` read
it. That is the tool to reach for: it needs no checkout, no `.env`, no
`node_modules` and no `tsx`, it works against staging and production, and it
compares the COMMANDS as well as the fields.

This script is still here for the one thing the command cannot do: check a
website-city working tree that has not been deployed anywhere yet. Run it before
the deploy; run `tmc contract drift` after.

WHY THIS EXISTS
---------------
`schema.py` is a mirror, and its own docstring says the server decides. That is
true and it is also how the mirror rots: nothing fails when the site adds a
field, because `--set-raw` and `--json` still get it through. The cost is
quieter than a break — a real field looks like a typo, `tmc schema asset` omits
it, and completion never offers it. So the mirror going stale is invisible from
inside this repo by construction, and the only thing that can see it is a diff
against the other one.

It found three the first time it was run: `asset.repoName` (the pack namespace
the game-uploads work added), and `collection.appId` and `collection.media`.

WHAT IT IS NOT
--------------
Not a test, and deliberately not wired into `python -m unittest discover`. It
needs website-city checked out next door, its `.env`, its `node_modules` and a
working `tsx` — none of which a build box running the CLI's own suite has, and
a test that skips itself on four conditions is a test that is always skipping.
Run it by hand after a release on the site, the way `tmc-app` runs
`contract:sync`.

USAGE
-----
    python3 scripts/schema-drift.py [--city ../website-city]

Exits 1 when the mirror and the registry disagree, so it can gate a release.

READING THE OUTPUT
------------------
"missing from the mirror" is the case to act on: a field the API takes and this
tool does not know about. "not on the server" is usually also real, but check
before deleting — a field may be accepted by the schema and dropped by the
handler before the writer sees it, which is exactly what `collection.ownerId`
does, and the mirror leaves that one out ON PURPOSE. Anything intentional
belongs in `EXPECTED_ABSENT` below, with the reason, or the next person to run
this deletes the comment explaining it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from tmc_cli import schema as S  # noqa: E402

# Fields the registry exposes and the mirror leaves out on purpose.
#
# One entry so far. `collectionFields` is `CreateCollectionInput.omit({ id: true })`
# while the other six canonical types also omit `ownerId` and `updateLastEdit`,
# so `ownerId` survives into the public schema — but `stripOwner()` in
# `handler.ts` deletes it from the body before the schema is ever reached. It is
# accepted and silently discarded, and a CLI flag for that is worse than none.
EXPECTED_ABSENT: dict[str, set[str]] = {
    "collection": {"ownerId"},
}

# The TypeScript that does the reading. Dropped into website-city's own
# `scripts/` so its path aliases (`~/lib/...`) resolve, and removed afterwards.
DUMP_TS = """\
/* Written by tmc-cli's scripts/schema-drift.py. Delete it if you find it. */
import { CONTENT_REGISTRY } from '~/lib/api/public/content'
import { RELATION_DEFS, RELATION_NAMES } from '~/lib/api/public/relations'

/* zod hides the shape behind whatever wrappers .strict()/.partial()/.extend()
   left on top, and how many there are depends on the type. Unwrap until a
   shape appears rather than guessing a depth. */
function shapeOf(schema: any): string[] {
    let s = schema
    for (let i = 0; i < 12 && s; i++) {
        if (s.shape) return Object.keys(s.shape)
        if (s._def?.shape) {
            const sh =
                typeof s._def.shape === 'function' ? s._def.shape() : s._def.shape
            if (sh) return Object.keys(sh)
        }
        s = s._def?.schema ?? s._def?.innerType ?? s.innerType ?? null
    }
    return []
}

const types: Record<string, { fields: string[]; relations: string[] }> = {}

for (const [name, cfg] of Object.entries(CONTENT_REGISTRY)) {
    const c = cfg as any
    /* The union of create and update. They differ — create tightens what the
       canonical input leaves optional — but either spelling is a field the
       CLI should know the name and type of. */
    const fields = new Set([
        ...shapeOf(c.createSchema),
        ...shapeOf(c.updateSchema),
    ])

    types[name] = {
        fields: [...fields].sort(),
        relations: RELATION_NAMES.filter((r) =>
            RELATION_DEFS[r].types.includes(name as any)
        ).sort(),
    }
}

console.log(JSON.stringify({ types }, null, 1))
"""


def registry(city: Path) -> dict:
    """Run the dump inside website-city and parse what it printed."""
    if not (city / "src/lib/api/public/content.ts").is_file():
        sys.exit(f"No content registry under {city} — is website-city checked out there?")

    env_file = city / ".env"
    if not env_file.is_file():
        sys.exit(
            f"No {env_file}. The registry imports `~/env`, which validates the "
            "environment on import and exits when it cannot."
        )

    # A name nothing else will pick up, in case a run is interrupted before the
    # `finally` removes it.
    script = city / "scripts" / "_tmc_cli_schema_dump.ts"
    script.write_text(DUMP_TS)

    try:
        done = subprocess.run(
            ["node", f"--env-file={env_file}", "--import", "tsx", str(script)],
            cwd=city,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:
        sys.exit("`node` is not on PATH.")
    except subprocess.TimeoutExpired:
        sys.exit("The registry dump did not finish within five minutes.")
    finally:
        script.unlink(missing_ok=True)

    if done.returncode != 0:
        sys.exit(f"The registry dump failed:\n{done.stderr.strip()[-2000:]}")

    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError:
        sys.exit(f"The registry dump printed something that is not JSON:\n{done.stdout[:2000]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--city",
        default=os.environ.get("TMC_WEBSITE_CITY", str(HERE.parent.parent / "website-city")),
        help="path to the website-city checkout (default: ../website-city)",
    )
    args = ap.parse_args()

    real = registry(Path(args.city).resolve())["types"]
    problems = 0

    only_server = sorted(set(real) - set(S.TYPES))
    only_mirror = sorted(set(S.TYPES) - set(real))

    if only_server:
        problems += 1
        print(f"types the API has and the mirror does not: {only_server}")
    if only_mirror:
        problems += 1
        print(f"types the mirror has and the API does not: {only_mirror}")

    for name in sorted(set(real) & set(S.TYPES)):
        spec = S.TYPES[name]
        server_fields = set(real[name]["fields"])
        mirror_fields = set(spec.field_names())

        missing = sorted(server_fields - mirror_fields - EXPECTED_ABSENT.get(name, set()))
        extra = sorted(mirror_fields - server_fields)

        server_rel = set(real[name]["relations"])
        mirror_rel = set(spec.relations)
        rel_missing = sorted(server_rel - mirror_rel)
        rel_extra = sorted(mirror_rel - server_rel)

        if missing or extra or rel_missing or rel_extra:
            problems += 1
            print(f"\n## {name}")
            if missing:
                print(f"   fields missing from the mirror : {missing}")
            if extra:
                print(f"   fields not on the server       : {extra}")
            if rel_missing:
                print(f"   relations missing              : {rel_missing}")
            if rel_extra:
                print(f"   relations the server does not have : {rel_extra}")

    if problems:
        print(f"\n{problems} disagreement(s). Update src/tmc_cli/schema.py.")
        return 1

    print(f"schema.py agrees with the registry — {len(real)} types.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
