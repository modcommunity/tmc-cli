# api-cli

Python CLI (`tmc`) for the TMC public content API. See `README.md` for usage.

## Layout

```
tmc                    runnable wrapper for a checkout
src/main.py            same, via `python src/main.py`
src/tmc_cli/
  cli.py               argparse wiring + dispatch; the command surface lives here
  context.py           what commands are handed (lazy client, output helpers)
  config.py            profiles in ~/.config/tmc/config.json, env overrides
  auth.py              Bearer / Jwt / Anonymous credentials (one header per attempt)
  ed25519.py           PKCS#8 PEM parse + signing; cryptography or RFC 8032 fallback
  http.py              transport: urllib, retries, multipart streaming
  client.py            the API as methods; batching lives here
  schema.py            local mirror of the server's content registry
  params.py            --set/--json → a typed JSON body
  output.py            table/json/csv/yaml rendering
  commands/            one module per surface
tests/
  mock_server.py       in-memory stand-in for /api/content
  test_cli.py          drives cli.main() against it over a real socket
```

## The source of truth

This tool mirrors the API; it does not define it. When something disagrees, the
website repo wins:

- `../website-city/docs/api/public-content-api.md` — the written contract. Note
  that it is prose maintained by hand and has drifted: it lists `srcUrl` on
  asset and mod (neither `Create*Input` has it), names the retired
  `API_RATE_5H_USER`-style env vars, and its relation-availability table
  predates `group` gaining `tags`. **The code wins over this file too.**
- `../website-city/src/lib/api/public/` — `handler.ts` (CRUD + bulk),
  `relation_handler.ts` (relations), `relations.ts` (which relation hangs off
  which type), `file.ts` (uploads), `auth.ts` (both credential shapes),
  `content.ts` (the type registry `schema.py` mirrors),
  `anon.ts` + `anon_handler.ts` (the unauthenticated read surface)
- `../website-city/src/types/<type>/post.ts` — the `Create*Input` schemas the
  API derives its field lists from

`schema.py` is a convenience (type coercion, typo detection, `tmc schema`), not
authority. Anything it doesn't know can still be sent with
`--allow-unknown-fields` or `tmc raw`, so a stale mirror is never a blocker.

**Which is exactly how it rots.** Nothing here fails when the site adds a
field; the field just looks like a typo, `tmc schema` omits it, and completion
never offers it. `scripts/schema-drift.py` is what can see that — it runs a
dump of `CONTENT_REGISTRY` inside the website-city checkout next door and diffs
the field and relation lists against `schema.py`:

```bash
python3 scripts/schema-drift.py              # ../website-city
python3 scripts/schema-drift.py --city ~/src/website-city
```

Run it after a release on the site. It is **not** part of `unittest discover`
and should not become part of it: it needs website-city, its `.env`, its
`node_modules` and a working `tsx`, and a test that skips itself on four
conditions is a test that is always skipping.

A field the mirror leaves out deliberately goes in that script's
`EXPECTED_ABSENT` with the reason. There is one: `collection.ownerId`, which is
in the public schema only because `collectionFields` omits `id` alone where the
other six canonical types also omit `ownerId` — and `stripOwner()` in
`handler.ts` deletes it from the body before the schema sees it. Accepted and
silently discarded is the one outcome not worth a flag.

## Behaviours that exist for a specific reason

Change these only with the reason in hand:

- **The base URL is the API origin, but the paths keep `/api`** (`config.py`).
  The four public surfaces moved to `api.moddingcommunity.com`, where the docs
  print the short form (`/content/...`). That spelling exists only because
  nginx's `location /` rewrites the prefix back on, so it is absent the moment
  this tool is pointed straight at a container or a dev checkout — which is half
  of what `--base-url` is for. `/api/content/...` is the route's real name in the
  app and answers on the API origin, on the apex and against a bare container,
  so it is the one spelling sent. The apex still answers at all because it
  PROXIES rather than redirects: a cross-origin redirect strips `Authorization`,
  so a stored profile from before the move keeps working rather than 401ing.
- **JWT assertions are signed per request attempt** (`http.py`). The `jti` is
  one-shot server-side; hoisting the header out of the retry loop turns every
  retry into a 400.
- **`release publish` sends an existing release back whole** (`release_cmd.py`).
  The server's `SyncReleases` writes `hidden: r.hidden ?? false`, so an update
  that omits the field un-hides a draft.
- **`release publish` uses POST, never PUT.** PUT on a relation is the complete
  set — a one-member PUT deletes every other release on the item.
- **Retries are limited to idempotent methods.** A POST that got no reply may
  still have landed.
- **Data on stdout, everything else on stderr.** Pipes have to stay clean.
- **`relation_write` keys its empty-body fallback on whether a request was
  sent**, not on the response being null. The other way round double-sends.
- **`--anon` sends no `Authorization` header, not an empty one** (`http.py`).
  `IsAnonRequest` branches on the header being ABSENT; a request presenting any
  credential — including a bad one — is the keyed surface's, and is answered
  401 rather than downgraded. So anonymous mode is a `Credential` that returns
  `None` rather than a flag threaded through the transport.
- **`--anon` beats a stored key** (`config.py`). It selects a different endpoint
  with a different answer shape, so silently upgrading to the key would make
  "what does the public see?" unanswerable.
- **Anonymous list filters are dropped locally, with a warning** (`client.py`).
  The server ignores an unknown query param, so a silently-dropped `--search`
  reads as a search that matched everything. The anonymous listing takes exactly
  two — `appId` and `official` — and the keyed listing takes neither, so
  `content_cmd.py` warns in that direction too.
- **Relation DELETE batches at 500, PUT/POST at 200.** Two different server caps
  (`RELATION_MEMBERS_MAX` bounds the body; `z.array(def.schema).max(200)` bounds
  the elements), and only a DELETE — which names keys, not members — can use the
  larger one. Batching a DELETE is safe; batching a PUT would have the second
  half delete the first.

## Testing

```bash
python -m unittest discover -s tests
```

The mock deliberately reproduces the server behaviours the CLI works around
(the `hidden` reset, jti consumption, the bulk caps, the anonymous surface's
three gates and its coded refusals) — if you relax one in the mock, you are
deleting the test, not fixing it.

`STATE.auth_headers` records the credential presented on each request, so the
anonymous surface's contract — no header at all — is asserted rather than
inferred from a 200.

Ed25519 is checked against RFC 8032 vectors and an openssl-generated PEM rather
than against itself.

## Conventions

- Standard library only. The tool has to run on a build box with no package
  index; `cryptography` is an optional speedup, never a requirement.
- Comments explain *why*, matching the website repo's style.
- New content types/fields: add to `schema.py`; new endpoints: `client.py` then
  a command module then `cli.py`.

## Known gaps

- `/api/content/server/integration/{stats,users}` sit under `/api/content` but
  belong to the **integration API** (`docs/api/integration-api.md`): a separate
  credential namespace (`tmci_`), scoped per-server, with its own scopes
  (`SERVER_STATS` / `SERVER_USERS`). They are not reachable with a content-API
  key, so `tmc` does not speak to them. Adding them means a second credential
  kind in `config.py`, not a new command module.

---

## External source code

`~/stack/external-study/` holds third-party source cloned **to be read** — Godot, the
Source engine, Momentum Mod, Shavit's `bhoptimer`, and the mod managers. Read-only,
never a dependency, never imported. Look there before designing something from
scratch; see [`external-study/README.md`](../external-study/README.md).
