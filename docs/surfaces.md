# Surfaces beyond `/api/content`

## Keyless: `tmc catalog` and `tmc defcon`

Both go through `Context.public_transport()`, an anonymous `Transport`; the profile's secret is never sent.

**`tmc catalog`** reads `/api/app/v1/{browse,content/<kind>/<id>,facets,reviews,apps,servers/lookup}` on the API origin.

- These routes are `auth: 'optional'`, but a `Bearer` that is sent is resolved as an app token (`ResolveAppToken`), and a publishing (`CLI`) credential is refused `403 wrong_credential`, so sending the profile's key would break reads that need none.
- List params are repeated keys (`parseQuery` + `normalizeArrayFields`), not the comma form `encode_params` uses for `/api/content`; `app_api.query_string` builds that dialect.
- Errors are flat, `{ok:false, code, message, retryAfter?}` (`apiError` in website-city's `src/lib/app-api/handler.ts`). `http.py` unwraps that, the nested `{error:{code,message}}` form, and tRPC's `{error:{json:{message,data:{code}}}}`.

**`tmc defcon`** reads `GET /api/status/defcon`, `/series` and `/mtr` on the API origin (website-city's REST mirrors, app-API envelope, anonymous).

- On a 404 there (a site from before the mirrors), or whenever `--site-url` is given, it asks tRPC `defcon.public.{status,series,mtr}` (superjson: `?input={"json":…}`, answer under `result.data.json`) on the website origin instead, because tRPC is refused on the API container.
- `config.site_url()` picks that origin: `--site-url` > `TMC_SITE_URL` > profile option `site_url` > base URL minus `api.`.
- Only the public procedures, never `defcon.admin.*`.
- `incidents --all` pages `/api/status/defcon/incidents` (90 days, open and resolved, `nextCursor`).
- The site's `show.nodes` / `show.incidents` switches are honoured even though `status` sends node rows regardless.

## The app API as the signed-in member

A second credential kind, held in the same profile as the content key (`Profile.app`: `base_url`, `access_token`, `refresh_token`, `expires_at`, `user`; same 0600 file, never printed). Commands: `tmc auth app-login | app-logout | app-whoami`, then `friends`, `party`, `presence`, `installs`, `subscriptions`, `reviews` (writes; `tmc review` is the content type), `stats`.

Site routes are `src/app/api/app/v1/…/route.ts` in website-city, schemas in `src/types/app-api/contract.ts` and `src/types/party/*`.

- **Login**: `auth/device` (`{client:{name,platform,version}, codeChallenge}`; PKCE is S256 only, `VerifyPkce` in `src/server/auth/app-token.ts`), then poll `auth/token` (`{deviceCode, codeVerifier}`). Pending and `slow_down` are **202** with the error envelope, a 2xx, so they come back as a `Response`, not an `ApiError`; `access_denied` / `expired_token` are 400. The poll waits the server's `interval`, adds 5s for good on `slow_down` (RFC 8628), and stops at `expiresIn`. `appuser_cmd._sleep` is the seam the tests patch.
- **Session**: `auth/refresh` (`{refreshToken}`, no bearer) **rotates**: the old refresh token is retired, and replaying a retired one revokes the family, so the new pair is saved immediately. `auth/revoke` revokes the whole lineage; `me` reads the user. `AppSession.request` refreshes at most once per call (before sending when `expires_at` has passed, or after a 401), then fails with the sign-in hint. A refresh refused 400/401 drops the stored pair. The session is bound to the base URL it came from and is never presented to another. `TMC_APP_TOKEN` is the env override (used as-is, no refresh).
- **Covered**: `friends` (list, requests, request, respond, cancel, remove); `presence` (GET, where `userIds` is one comma-joined string unlike the rest of this API, and POST); `party` (mine, `[id]`, create, invite, invites, invite/respond, join, leave, kick, ready, start); `installs` (GET/POST/PATCH/DELETE, DELETE takes a JSON body) and `installs/items` (POST/PATCH/DELETE); `subscriptions` (GET/POST) and `subscriptions/prefs`; `reviews/write` (POST write, DELETE, PUT vote); `stats/top`, `stats/me`, `stats/defs`.
- **Left out on purpose**: party cancel/connected/end/force-start/heartbeat/role/transfer/reserve*/`[id]/ready` (game-loop and host-admin plumbing), `PATCH me` (settings sync), `downloads`, `packs`, `play/*`, `report`, `avatar`, `stats/submit`, `auth/handoff` (a game token's, not a device's).

## Known gap: the integration API

`/api/content/server/integration/{stats,users}` sit under `/api/content` but belong to the integration API (website-city's `docs/api/integration-api.md`): a separate credential namespace (`tmci_`), scoped per server, with its own scopes (`SERVER_STATS` / `SERVER_USERS`). They are not reachable with a content-API key, so `tmc` does not speak to them. Adding them means another credential kind in `config.py`, not a new command module.
