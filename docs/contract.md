# The source of truth, the contract, and drift

This tool mirrors the API; it does not define it. When something disagrees, the website repo wins:

- `../website-city/docs/api/public-content-api.md`: the written contract. It is prose maintained by hand and has drifted: it lists `srcUrl` on asset and mod (neither `Create*Input` has it), names the retired `API_RATE_5H_USER`-style env vars, and its relation-availability table predates `group` gaining `tags`. **The code wins over this file too.**
- `../website-city/src/lib/api/public/`: `handler.ts` (CRUD and bulk), `relation_handler.ts` (relations), `relations.ts` (which relation hangs off which type), `file.ts` (uploads), `auth.ts` (both credential shapes), `content.ts` (the type registry `schema.py` mirrors), `anon.ts` and `anon_handler.ts` (the unauthenticated read surface).
- `../website-city/src/types/<type>/post.ts`: the `Create*Input` schemas the API derives its field lists from.

## `schema.py` is a convenience, and that is how it rots

`schema.py` gives type coercion, typo detection and `tmc schema`; it is not authority. Anything it does not know can still be sent with `--allow-unknown-fields` or `tmc raw`, so a stale mirror is never a blocker. Which is exactly how it rots: nothing here fails when the site adds a field; the field just looks like a typo, `tmc schema` omits it, and completion never offers it.

## `tmc contract`: the first thing to reach for

The site publishes its registry and the command grammar of its own web console at `<base>/api/content/spec`, with no key. `contract.py` fetches it, caches it under the config directory, and overlays it on `TYPES`:

```bash
tmc contract sync --base-url https://moddingcommunity.com
tmc contract drift          # exits 3 when the two disagree
```

Precedence after a sync: **field existence, type and requiredness come from the site**; **notes and enums stay the mirror's** (the contract carries neither); **commands stay the mirror's**, because argparse builds the parser at import time and `--help` must work offline. So a field the site added is corrected today, and a command it grew needs a release, which is what `drift` says.

`drift` separates three things, and only the first two fail it: a field the site has that this build lacks, a command the site has that this build lacks, and a field this build is stricter about. The last bucket is permanent and intentional (most id columns are `z.number()` with no `.int()` upstream); folding it into the exit code would make the check fail forever and stop being read.

## `scripts/schema-drift.py`

It predates `tmc contract` and still works: it dumps `CONTENT_REGISTRY` from a website-city checkout next door rather than fetching the published contract.

```bash
python3 scripts/schema-drift.py              # ../website-city
python3 scripts/schema-drift.py --city ~/src/website-city
```

Prefer `tmc contract drift` unless you need to check a checkout that has not been deployed yet, the one thing the script can do and the command cannot. Neither is part of `unittest discover`, and neither should become part of it: the script needs website-city, its `.env`, its `node_modules` and a working `tsx`, and the command needs a reachable site; a test that skips itself on four conditions is always skipping. `tests/test_contract.py` covers everything about the contract that is decidable offline.

A field the mirror leaves out deliberately goes in the script's `EXPECTED_ABSENT` with the reason. There is one: `collection.ownerId`, which is in the public schema only because `collectionFields` omits `id` alone where the other six canonical types also omit `ownerId`, and `stripOwner()` in `handler.ts` deletes it from the body before the schema sees it. Accepted and silently discarded is the one outcome not worth a flag.

## The web console

"The web version" is the web console at `/console` on the site (`src/lib/console/` and `src/types/console/` in website-city): the same `tmc` grammar, run as the signed-in session. The two are kept in step by the contract (`/api/content/spec`, website-city's `docs/api/cli-contract.md`), whose `commands` are `ConsoleSpec()`.

This repo cannot change the console. A command added here shows up in `tmc contract drift` as "this build has, the site lacks" (non-failing) until the console grows it. `catalog` and `defcon` exist on both sides (website-city `src/lib/console/public.ts`, run in process); its `spec.test.ts` pins their positionals and flags to `_build_catalog` / `_build_defcon` here, so changing either tree means changing that table too. Neither is CLI-only by nature, so they are deliberately not in `CLI_ONLY` (`contract.py`).
