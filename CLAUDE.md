# CLAUDE.md — tmc-cli

`tmc`, the Python CLI for the TMC public content API, plus the keyless catalog and status reads and the app API as a signed-in member. It mirrors website-city's API and does not define it. See `README.md` for usage.

## Layout

```
tmc                    runnable wrapper for a checkout
src/main.py            same, via `python src/main.py`
src/tmc_cli/
  cli.py               argparse wiring + dispatch; the command surface lives here
  context.py           what commands are handed (lazy client, output helpers, public_transport)
  config.py            profiles in ~/.config/tmc/config.json, env overrides, site_url()
  auth.py              Bearer / Jwt / AppToken / Anonymous credentials (one header per attempt)
  app_api.py           app-user session: stored pair, refresh-once, app API query dialect
  ed25519.py           PKCS#8 PEM parse + signing; cryptography or RFC 8032 fallback
  http.py              transport: urllib, retries, multipart streaming, error unwrapping
  client.py            the API as methods; batching lives here
  schema.py            local mirror of the server's content registry
  contract.py          the site's own answer, fetched and overlaid on that; CLI_ONLY
  params.py            --set/--json → a typed JSON body
  output.py            table/json/csv/yaml rendering
  commands/            one module per surface (catalog_cmd: app API public reads;
                       defcon_cmd: status data; appuser_cmd: device login and the
                       per-member routes, game-client-only ones excluded; release_cmd, relation_cmd, contract_cmd, …)
scripts/schema-drift.py  registry drift against a website-city checkout
tests/
  mock_server.py       in-memory stand-in for /api/content, the app API (public and
                       as a member), and defcon REST + tRPC
  test_cli.py          drives cli.main() against it over a real socket
  test_contract.py     the published contract, offline
  test_public.py       open, catalog, defcon: the surfaces that take no key
  test_app_user.py     app-login (pending/slow_down/denied/expiry), refresh, every app-user group
```

## Run and test

```bash
python -m unittest discover -s tests
tmc contract drift          # against a live site; exits 3 on drift
```

Neither drift check (`tmc contract drift`, `scripts/schema-drift.py`) belongs in `unittest discover`: both need things a test run does not have ([docs/contract.md](docs/contract.md#scriptsschema-driftpy)).

## Rules

- The website repo wins every disagreement; the code there wins over its own prose docs. ([contract](docs/contract.md))
- `schema.py` is a convenience, not authority; check it with `tmc contract drift` before trusting it. Strictness differences are deliberately non-failing. ([contract](docs/contract.md#tmc-contract-the-first-thing-to-reach-for))
- Commands stay the mirror's, because `--help` must work offline; a command the site grew needs a release. ([contract](docs/contract.md#tmc-contract-the-first-thing-to-reach-for))
- `catalog` and `defcon` flags are pinned by website-city's console `spec.test.ts`; change both trees together. ([contract](docs/contract.md#the-web-console))
- Keyless surfaces go through `Context.public_transport()`; the profile's key is never sent to them (the app API refuses a publishing credential). ([surfaces](docs/surfaces.md))
- App-user refresh rotates: save the new pair immediately; refresh at most once per call; never present a session to another base URL. ([surfaces](docs/surfaces.md#the-app-api-as-the-signed-in-member))
- Paths keep `/api` even on the API origin. ([behaviours](docs/behaviours.md))
- Sign JWT assertions per attempt, never once outside the retry loop. ([behaviours](docs/behaviours.md))
- `release publish` uses POST, never PUT, and sends an existing release back whole. ([behaviours](docs/behaviours.md))
- Retry only idempotent methods. ([behaviours](docs/behaviours.md))
- Data on stdout, everything else on stderr.
- `--anon` sends no `Authorization` header at all, beats a stored key, and drops unsupported list filters with a warning. ([behaviours](docs/behaviours.md))
- Relation DELETE batches at 500, PUT/POST at 200; never batch a PUT. ([behaviours](docs/behaviours.md))
- The mock reproduces the server quirks on purpose; relaxing one deletes a test. ([behaviours](docs/behaviours.md#what-the-tests-hold-the-code-to))
- Standard library only; `cryptography` is an optional speedup, never a requirement. The tool has to run on a build box with no package index.
- New content types or fields: `schema.py`. New endpoints: `client.py`, then a command module, then `cli.py`.
- Comments explain why, matching the website repo's style.
- Third-party source cloned to be read lives in `/extra/external/` (see its README): look there before designing something from scratch; never a dependency.

## Read before

| Read | Before |
| --- | --- |
| [docs/contract.md](docs/contract.md) | Touching `schema.py`, `contract.py`, the drift script, or anything the web console shares |
| [docs/surfaces.md](docs/surfaces.md) | Working on `catalog`, `defcon`, the app-user commands (and which app routes are deliberately not commands), or the integration API gap |
| [docs/behaviours.md](docs/behaviours.md) | Changing transport, auth, retries, batching, `--anon`, release publishing, or the mock |
