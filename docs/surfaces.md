# Surfaces beyond `/api/content`

## Keyless: `tmc catalog` and `tmc defcon`

Both go through `Context.public_transport()`, an anonymous `Transport`; the profile's secret is never sent.

**`tmc catalog`** reads `/api/app/v1/{browse,content/<kind>/<id>,facets,reviews,apps,apps/<id>/build,servers/lookup}` on the API origin. `tmc catalog listing` is the one read from the content API instead: `/api/content/server/list`, the anonymous address-only server listing a game's server browser starts from (paged by number; same per-address quota as every anonymous read).

- These routes are `auth: 'optional'`, but a `Bearer` that is sent is resolved as an app token (`ResolveAppToken`), and a publishing (`CLI`) credential is refused `403 wrong_credential`, so sending the profile's key would break reads that need none.
- List params are repeated keys (`parseQuery` + `normalizeArrayFields`), not the comma form `encode_params` uses for `/api/content`; `app_api.query_string` builds that dialect.
- Errors are flat, `{ok:false, code, message, retryAfter?}` (`apiError` in website-city's `src/lib/app-api/handler.ts`). `http.py` unwraps that, the nested `{error:{code,message}}` form, and tRPC's `{error:{json:{message,data:{code}}}}`.

**`tmc defcon`** reads `GET /api/status/defcon`, `/series` and `/mtr` on the API origin (website-city's REST mirrors, app-API envelope, anonymous).

- On a 404 there (a site from before the mirrors), or whenever `--site-url` is given, it asks tRPC `defcon.public.{status,series,mtr}` (superjson: `?input={"json":…}`, answer under `result.data.json`) on the website origin instead, because tRPC is refused on the API container.
- `config.site_url()` picks that origin: `--site-url` > `TMC_SITE_URL` > profile option `site_url` > base URL minus `api.`.
- website-status serves the same REST mirror on its own host (status.moddingcommunity.com); city's API origin forwards to it, so the default still works. `--status-url` > `TMC_STATUS_URL` > profile option `status_url` aims the REST reads there (no guessed default). `tmc defcon summary` (`/api/status/summary`) exists only on that host. `/api/status/ping` is a probe, not wrapped.
- Only the public procedures, never `defcon.admin.*`.
- `incidents --all` pages `/api/status/defcon/incidents` (90 days, open and resolved, `nextCursor`).
- The site's `show.nodes` / `show.incidents` switches are honoured even though `status` sends node rows regardless.

## The app API as the signed-in member

A second credential kind, held in the same profile as the content key (`Profile.app`: `base_url`, `access_token`, `refresh_token`, `expires_at`, `user`; same 0600 file, never printed). Commands: `tmc auth app-login | app-logout | app-whoami`, then `friends`, `party`, `presence`, `installs`, `subscriptions`, `reviews` (writes; `tmc review` is the content type), `stats`, `account`, `report`.

Site routes are `src/app/api/app/v1/…/route.ts` in website-city, schemas in `src/types/app-api/contract.ts` and `src/types/party/*`.

- **Login**: `auth/device` (`{client:{name,platform,version}, codeChallenge}`; PKCE is S256 only, `VerifyPkce` in `src/server/auth/app-token.ts`), then poll `auth/token` (`{deviceCode, codeVerifier}`). Pending and `slow_down` are **202** with the error envelope, a 2xx, so they come back as a `Response`, not an `ApiError`; `access_denied` / `expired_token` are 400. The poll waits the server's `interval`, adds 5s for good on `slow_down` (RFC 8628), and stops at `expiresIn`. `appuser_cmd._sleep` is the seam the tests patch.
- **Session**: `auth/refresh` (`{refreshToken}`, no bearer) **rotates**: the old refresh token is retired, and replaying a retired one revokes the family, so the new pair is saved immediately. `auth/revoke` revokes the whole lineage; `me` reads the user. `AppSession.request` refreshes at most once per call (before sending when `expires_at` has passed, or after a 401), then fails with the sign-in hint. A refresh refused 400/401 drops the stored pair. The session is bound to the base URL it came from and is never presented to another. `TMC_APP_TOKEN` is the env override (used as-is, no refresh).
- **Covered**: `friends` (list, requests, request, respond, cancel, remove); `presence` (GET, where `userIds` is one comma-joined string unlike the rest of this API, and POST); `party` (mine, `[id]`, create, invite, invites, invite/respond, join, leave, kick, ready, start, `[id]/ready` as `round`, and the managers' cancel, end, force-start, role, transfer, plus booking: `[id]/reserve/offer` as `offer`, reserve, reserve/cancel); `installs` (GET/POST/PATCH/DELETE, DELETE takes a JSON body) and `installs/items` (POST/PATCH/DELETE); `subscriptions` (GET/POST) and `subscriptions/prefs`; `reviews/write` (POST write, DELETE, PUT vote); `stats/top`, `stats/me`, `stats/defs`; `account` (`me` GET settings and PATCH as `set`, which sends only the keys named because the route is a partial merge; `downloads` GET; `avatar` GET/PUT); `report` (POST; title 3..120 and content 10..4000 are checked locally because the route allows ten an hour).
- **Not commands, on purpose**: these belong to a game client or the desktop app, and a terminal calling them would either lie to the site or do nothing useful.
  - `party/heartbeat`: proves a game is still running for a party member; the site reaps a member after 150 s without one. A CLI beating would keep a player "present" who is not in the game.
  - `party/connected`: "my game has reached the server", which lets the ready round start. Only the game knows that.
  - `play/launch`: resolves a Play press into a native URI or a web-player boot descriptor (with a 60 s handoff code) for the app to act on; there is nothing for a terminal to launch.
  - `play/scope-key`: mints the per-server player key a client hands to the server it is joining (`docs/api/avatar-protocol.md`); useful only to the joining client, and each call writes a row.
  - `stats/submit`: a player's own client filing self-reported figures for its game; a terminal would only forge a diary.
  - `update/<target>/<arch>/<current>`: the desktop app's self-updater feed (unauthenticated, like `/version`); only the app's updater asks it.
  - `auth/handoff`: the web-player loader redeems a one-use code for a `GAME` token; `auth: none`, no device session involved.
  - `downloads` POST/DELETE: the app reporting its own download queue, keyed on the reporting device's token; the CLI's device has no queue. The GET (all devices) is `tmc account downloads`.
  - `packs`: the registry half of pack publishing (`claim`/`complete`/`fail`), which ships switched off (`uploads.packsEnabled`) and is driven by the site's upload form and worker (`src/worker/jobs/upload/pack-publish.ts`), which move the bytes. A bare `claim` from a terminal permanently spends a version number on a pack with no files; it belongs with whatever moves the bytes, not in a registry-only command.

## Known gap: the integration API

`/api/content/server/integration/{stats,users}` sit under `/api/content` but belong to the integration API (website-city's `docs/api/integration-api.md`): a separate credential namespace (`tmci_`), scoped per server, with its own scopes (`SERVER_STATS` / `SERVER_USERS`). They are not reachable with a content-API key, so `tmc` does not speak to them. Adding them means another credential kind in `config.py`, not a new command module.
