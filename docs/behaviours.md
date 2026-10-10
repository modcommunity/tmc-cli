# Behaviours that exist for a specific reason

Change these only with the reason in hand.

- **The base URL is the API origin, but the paths keep `/api`** (`config.py`). The public surfaces moved to `api.moddingcommunity.com`, where the docs print the short form (`/content/...`). That spelling exists only because nginx's `location /` rewrites the prefix back on, so it is absent the moment this tool points straight at a container or a dev checkout, which is half of what `--base-url` is for. `/api/content/...` is the route's real name in the app and answers on the API origin, on the apex and against a bare container, so it is the one spelling sent. The apex still answers because it proxies rather than redirects: a cross-origin redirect strips `Authorization`, so a stored profile from before the move keeps working rather than 401ing.
- **JWT assertions are signed per request attempt** (`http.py`). The `jti` is one-shot server-side; hoisting the header out of the retry loop turns every retry into a 400.
- **`release publish` sends an existing release back whole** (`release_cmd.py`). The server's `SyncReleases` writes `hidden: r.hidden ?? false`, so an update that omits the field un-hides a draft.
- **`release publish` uses POST, never PUT.** PUT on a relation is the complete set; a one-member PUT deletes every other release on the item.
- **Retries are limited to idempotent methods.** A POST that got no reply may still have landed.
- **Data on stdout, everything else on stderr.** Pipes have to stay clean.
- **`relation_write` keys its empty-body fallback on whether a request was sent**, not on the response being null. The other way round double-sends.
- **`--anon` sends no `Authorization` header, not an empty one** (`http.py`). `IsAnonRequest` branches on the header being absent; a request presenting any credential, including a bad one, belongs to the keyed surface and is answered 401 rather than downgraded. So anonymous mode is a `Credential` that returns `None`, not a flag threaded through the transport.
- **`--anon` beats a stored key** (`config.py`). It selects a different endpoint with a different answer shape, so silently upgrading to the key would make "what does the public see?" unanswerable.
- **Anonymous list filters are dropped locally, with a warning** (`client.py`). The server ignores an unknown query param, so a silently dropped `--search` reads as a search that matched everything. The anonymous listing takes exactly two (`appId` and `official`) and the keyed listing takes neither, so `content_cmd.py` warns in that direction too.
- **Relation DELETE batches at 500, PUT/POST at 200.** Two different server caps (`RELATION_MEMBERS_MAX` bounds the body; `z.array(def.schema).max(200)` bounds the elements), and only a DELETE, which names keys rather than members, can use the larger one. Batching a DELETE is safe; batching a PUT would have the second half delete the first.

## What the tests hold the code to

The mock (`tests/mock_server.py`) deliberately reproduces the server behaviours above that the CLI works around: the `hidden` reset, jti consumption, the bulk caps, the anonymous surface's three gates and its coded refusals. Relaxing one in the mock deletes the test rather than fixing anything.

`STATE.auth_headers` records the credential presented on each request, so the anonymous surface's contract (no header at all) is asserted rather than inferred from a 200. Ed25519 is checked against RFC 8032 vectors and an openssl-generated PEM rather than against itself.
