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
  contract.py          the site's OWN answer, fetched and overlaid on that
  params.py            --set/--json → a typed JSON body
  output.py            table/json/csv/yaml rendering
  commands/            one module per surface
                       (catalog_cmd: app API public reads; defcon_cmd: /status data)
tests/
  mock_server.py       in-memory stand-in for /api/content (+ app API reads, defcon REST + tRPC)
  test_cli.py          drives cli.main() against it over a real socket
  test_contract.py     the published contract, offline
  test_public.py       open, catalog, defcon — the surfaces that take no key
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
never offers it.

**`tmc contract` is the answer to that, and the first thing to reach for.** The
site publishes its registry and the command grammar of its own web console at
`<base>/api/content/spec`, with no key. `contract.py` fetches it, caches it under
the config directory, and overlays it on `TYPES`:

```bash
tmc contract sync --base-url https://moddingcommunity.com
tmc contract drift          # exits 3 when the two disagree
```

Precedence after a sync: **field existence, type and requiredness come from the
site**; **notes and enums stay the mirror's** (the contract carries neither);
**commands stay the mirror's**, because argparse builds the parser at import
time and `--help` must work offline. So a field the site added is corrected
today and a command it grew needs a release — which is what `drift` says.

`drift` separates three things deliberately, and only the first two fail it: a
field the site has that this build lacks, a command the site has that this build
lacks, and a field this build is STRICTER about. That last bucket is permanent
and intentional — most id columns are `z.number()` with no `.int()` upstream —
and folding it into the exit code would make the check fail forever and stop
being read.

`scripts/schema-drift.py` predates this and still works — it dumps
`CONTENT_REGISTRY` from a website-city checkout next door rather than fetching
the published contract:

```bash
python3 scripts/schema-drift.py              # ../website-city
python3 scripts/schema-drift.py --city ~/src/website-city
```

Prefer `tmc contract drift` unless you need to check a checkout that has not been
deployed yet, which is the one thing the script can do and the command cannot.
Neither is part of `unittest discover` and neither should become part of it: the
script needs website-city, its `.env`, its `node_modules` and a working `tsx`,
and the command needs a reachable site — a test that skips itself on four
conditions is a test that is always skipping. `tests/test_contract.py` covers
everything about the contract that is decidable offline.

A field the mirror leaves out deliberately goes in that script's
`EXPECTED_ABSENT` with the reason. There is one: `collection.ownerId`, which is
in the public schema only because `collectionFields` omits `id` alone where the
other six canonical types also omit `ownerId` — and `stripOwner()` in
`handler.ts` deletes it from the body before the schema sees it. Accepted and
silently discarded is the one outcome not worth a flag.

## The web version

"The web version" is the **web console** at `/console` on the site
(`src/lib/console/` + `src/types/console/` in website-city): the same `tmc`
grammar, run as the signed-in session. The two are kept in step by the contract
(`/api/content/spec`, `docs/api/cli-contract.md`), whose `commands` are
`ConsoleSpec()`. This repo cannot change the console; a command added here shows
up in `tmc contract drift` as "this build has, the site lacks" (non-failing)
until the console grows it. `catalog` and `defcon` are in that state today —
neither is CLI-only by nature, so they are deliberately NOT in `CLI_ONLY`.

## Beyond /api/content

Two more surfaces, both **keyless**, both through `Context.public_transport()`
(an anonymous `Transport`; the profile's secret is never sent):

- **`tmc catalog`** → `/api/app/v1/{browse,content/<kind>/<id>,facets,reviews,apps,servers/lookup}`
  on the API origin. These routes are `auth: 'optional'`, but a `Bearer` that is
  sent is resolved as an app token (`ResolveAppToken`) and a publishing (`CLI`)
  credential is refused `403 wrong_credential` — so sending the profile's key
  would break reads that need none. List params are REPEATED keys (`parseQuery` +
  `normalizeArrayFields`), not the comma form `encode_params` uses for
  `/api/content` — `catalog_cmd._get` builds its own query for that reason.
  Errors are `{ok:false, error:{code,message}}`; `http.py` unwraps that and
  tRPC's `{error:{json:{message,data:{code}}}}`.
- **`tmc defcon`** → `GET /api/status/defcon`, `/series`, `/mtr` on the **API
  origin** (website-city's REST mirrors, app-API envelope, anonymous). On a 404
  there (a site from before the mirrors), or whenever `--site-url` is given, it
  asks tRPC `defcon.public.{status,series,mtr}` (superjson:
  `?input={"json":…}`, answer under `result.data.json`) on the **website
  origin** instead: tRPC is refused on the API container. `config.site_url()`
  picks that origin: `--site-url` > `TMC_SITE_URL` > profile option `site_url` >
  base URL minus `api.`. Only the public procedures — never `defcon.admin.*`.
  The site's `show.nodes` / `show.incidents` switches
  are honoured even though `status` sends node rows regardless.

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

- **Per-user app API** (`/api/app/v1` friends, parties, presence, installs,
  subscriptions, downloads, writing reviews, `stats/me`) takes an app-user token
  from the device flow (`auth/device` → `auth/token`, `tmca_…`), not a content
  key. Same shape of gap as the integration API below: a credential kind, not a
  command module. Leaderboards (`stats/top`) are game-scoped (`allowGame`) and
  need that token or a game's.
- **Defcon incident history is not public.** `defcon.public.status` carries only
  OPEN alerts (max 20). A history needs a new public procedure (or REST route)
  in website-city returning resolved `DefconAlert` rows for public+enabled
  monitors: `{id, createdAt, resolvedAt, status, monitorName, nodeName,
  message}`, cursor-paged, gated on `defcon.statusPublic` and
  `defcon.statusShowIncidents`, node named by `displayName ?? location` (never
  `host`), 90-day cap. Then `tmc defcon incidents --all`.
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
