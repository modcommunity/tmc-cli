A command-line tool for TMC's public API. It creates,
edits and deletes your assets, mods, servers, articles, communities, collections
and groups — and, because that is what the API is actually used for, it uploads
files and cuts releases in one command.

Supports **both** authentication modes the API accepts: bearer tokens and
Ed25519 signed assertions (JWT mode) — and reading public items with **no key at
all** (`--anon`).

**No dependencies.** Python 3.10+ and the standard library. `cryptography` is
used for signing if it happens to be installed, and the bundled RFC 8032
implementation is used if it isn't — same signatures either way.

## Install

```bash
pip install .                 # gives you `tmc` on PATH
pip install '.[fast]'         # …plus `cryptography` for faster JWT signing
```

Or just run it from a checkout — there is nothing to build:

```bash
./tmc --help
python src/main.py --help
```

## Log in
Keys are managed under **Account → API Keys** on the site.

```bash
# Bearer key (the default kind). Omit --token to be prompted without echo.
tmc auth login --token tmc_xxxxxxxxxxxx

# JWT key: you hold the Ed25519 private key, the site holds only the public half.
tmc auth login --jwt --key-id tmcak_xxxxxxxx --private-key ~/keys/tmc.pem --copy-key

# Several sites or several keys:
tmc auth login --profile staging --base-url https://staging.example.com --token tmc_…
tmc auth use staging
```

Credentials live in `~/.config/tmc/config.json` (mode `0600`). `TMC_TOKEN`,
`TMC_KEY_ID`, `TMC_PRIVATE_KEY_FILE`, `TMC_BASE_URL` and `TMC_PROFILE` work too,
which is usually what you want in CI.

Check what you have:

```bash
tmc auth whoami       # which key, and what the server lets it do
tmc auth doctor       # crypto backend, config permissions, connectivity
```

`whoami` establishes permissions with three deliberately harmless probe requests
(a read, a create that cannot validate, a delete of an empty id list). Nothing is
created or deleted. Pass `--read-only` to send only the read.

## Reading without a key
`GET` on a content item answers with no `Authorization` header at all, for items
that are completely public. `--anon` is how you ask for that:

```bash
tmc mod get 123 --anon
tmc mod list --anon --app 4 -o json
```

What comes back is a **summary** — id, name, slug, path, description, app,
categories, engagement counts, plus a live `server` block for servers — not the
record. It is a genuinely different endpoint, so a few things follow:

- **Seven types only:** `asset`, `mod`, `server`, `community`, `article`,
  `collection`, `group`. Relations, files, comments and reviews always need a key.
- **One list filter,** `--app`, and only on the four types that have an app.
  `--search`, `--tag`, `--category`, `--mine` are dropped with a warning rather
  than silently ignored — the server would ignore them, and a filter that did
  nothing looks exactly like one that matched everything.
- **Read-only.** Any write is refused before the request leaves, since sending it
  would come back as a 401 that blames a key you did not send.
- **Its own quota,** counted per source address and deliberately small. A key is
  free and has a much larger budget.
- **Three gates,** any of which can turn it off: the site's own switch, the item
  being completely public (not hidden, not NSFW, no hidden parent), and the
  item's team not having set `apiPublic: false`. Only the third is told apart
  from "no such item" — you get a `403 api_disabled` rather than a `404`.

`tmc mod update 5 --set apiPublic=false` is the other side of that switch.
(Servers are the one type that does not have the field.)

## Everyday use
```bash
tmc mod list --mine --all                       # every page of your own mods
tmc mod list --search rust --tag pvp -o json
tmc mod get 42
tmc mod update 42 --set description="Now with fewer bugs"
tmc mod delete 42
```

Creating something, with the body read from a file and the icon uploaded on the
way through:

```bash
tmc mod create \
  --set name="My Mod" --set appId=1 \
  --content-file README.md \
  --icon assets/icon.png \
  --tag tooling --tag gamedev
```

`--set` coerces to the field's real type (the API's schemas are strict and do not
coerce, so `appId=1` has to become a number, not `"1"`). `--set-json` takes a JSON
literal, `--set-file` reads a value from disk, and `--json` supplies a whole body
that `--set` flags are then applied on top of.

Unknown field names are caught locally, with the near-miss:

```
$ tmc mod create --set tgs=pvp …
error: 'mod' has no field 'tgs'. Did you mean 'tags'?
```

Run `tmc schema mod` for the field list, or `tmc schema` for everything.

## Releases and files
This is the part worth having a tool for. `release publish` uploads the files,
collects their ids, reads the existing release set and writes the release with
everything attached — in the right order, without disturbing your other releases:

```bash
tmc release publish --mod 5 \
  --version 1.2.0 \
  --title "Bug fixes" \
  --content-file CHANGELOG.md \
  --file 'dist/*.zip' --file dist/checksums.txt
```

Run it again with the same `--version` and it **updates** that release, merging
the new files into the existing set (`--replace-files` to swap them out instead).
Other releases on the item are never touched, and a hidden release stays hidden —
the API resets `hidden` when an update omits it, so publish always sends it back
explicitly.

```bash
tmc release list  --mod 5
tmc release files --mod 5 --version 1.2.0
tmc release rm    --mod 5 --version 1.0.0
```

Files on their own:

```bash
tmc file upload 'dist/*.zip'          # batched past the API's 20-per-request cap
tmc file upload big.zip --raw         # body-is-the-file form
tmc file get <id>
tmc file update <id> --title "Renamed"
tmc file download <id> -O ./here/
tmc file rm <id>
```

Per-file size limits are the key owner's own — 20 MB standard, 1 GB for
supporters. Anything over 20 MB gets a local warning; the server has the final
say and answers `413`.

## Relations
Relations (`tags`, `media`, `releases`, `links`, `sources`, `items`) can be
managed without re-sending the whole item. The verbs say what they do:

| Command         | Method | Meaning                                   |
| --------------- | ------ | ----------------------------------------- |
| `tmc rel add`   | POST   | merge these in, leave everything else     |
| `tmc rel set`   | PUT    | **this is now the complete set**          |
| `tmc rel rm`    | DELETE | drop the ones you named                   |
| `tmc rel clear` | DELETE | drop all of them                          |

```bash
tmc tags  add mod 5 pvp vanilla
tmc tags  rm  mod 5 pvp
tmc media add mod 5 --file shot.png --title Screenshot   # uploads, then attaches
tmc media add mod 5 --url https://example.com/a.png
tmc links add mod 5 https://github.com/me/repo --type GITHUB

tmc rel get mod 5 releases
tmc rel add mod 5 sources --member '{"sourceId": 3, "path": "/mods/mine"}'
tmc rel set mod 5 media --from-file gallery.json
```

`tmc template media` prints a starter array for `--from-file`.

The same warning the API's docs give applies to the item body: setting `--tag`,
or a `media`/`releases` array via `--set-json`, **replaces** that relation
wholesale. The CLI says so before it sends.

## Bulk
```bash
tmc mod create --from-file mods.json      # any length; batched into 25s
tmc mod update --from-file edits.json     # each element needs an "id"
tmc mod delete 1 2 3 …                    # batched into 100s
tmc mod list --mine --all -o ids | xargs tmc mod delete
```

Bulk writes are **not transactional** server-side. If a batch fails partway, the
CLI reports the failing element's index and lists what did get written, so you
can resume rather than start over.

## Output
`-o table` (default), `json`, `jsonl`, `csv`, `tsv`, `yaml`, `ids`. `--field
id,name` keeps only the columns you want.

Data goes to **stdout**, progress and errors to **stderr**, so pipes stay clean:

```bash
tmc mod list --mine --all -o ids | xargs -n1 tmc mod get -o json
```

Exit codes: `0` ok, `2` usage, `3` auth (401/403), `4` not found, `5` validation
(400/413), `6` rate limited, `7` server error, `8` network.

## What it handles for you
- **A fresh JWT assertion per request attempt.** A `jti` is consumed on first
  use, so a retry that resent the same header would be a `400`. Signing happens
  inside the retry loop.
- **Rate limits.** A `429` carries `retry-after` where the API sets one and
  names its own wait in prose where it does not; the CLI waits exactly that long,
  up to `--retry-wait-max` (120s), then tells you how long is left instead of
  hanging a build. 5xx and connection failures get exponential backoff, and only
  on idempotent methods — a POST is never silently repeated.
- **Every server cap.** 25 items per bulk write, 100 per bulk delete, 200
  relation members on a PUT/POST, 500 keys on a relation DELETE, 20 upload
  parts. Pass whatever you have; it gets batched.
- **Pagination.** `--all` walks it, `--max` stops early.
- **Streaming uploads.** A 1 GB file is read from disk in chunks, not into RAM.

## Escape hatch
This CLI mirrors the API's field list locally so it can catch typos before
spending a request — which means it can be one deploy behind. Nothing is ever
blocked by that:

```bash
tmc mod create --set someNewField=1 --allow-unknown-fields
tmc raw PUT /api/content/mod/5/releases --json @releases.json
```

## Shell completion

```bash
tmc completion bash > /etc/bash_completion.d/tmc
tmc completion zsh  > "${fpath[1]}/_tmc"
tmc completion fish > ~/.config/fish/completions/tmc.fish
```

## Tests

```bash
python -m unittest discover -s tests
```

75 tests run the real CLI against an in-process mock of the API over a real
socket, covering both auth modes (including Ed25519 against RFC 8032 vectors and
an openssl-generated key), the anonymous surface, batching, relation
merge/replace semantics, the release workflow and the retry path.

## Reference

The API this speaks to is documented in the website repo at
`docs/api/public-content-api.md`; the implementation is under
`src/lib/api/public/`.
