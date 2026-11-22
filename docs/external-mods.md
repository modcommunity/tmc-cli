# External mods: `tmc mods`

`tmc mods add <link-or-file>` installs a mod that lives outside the site.

## Design choice

The app's logic (`src-tauri/core/src/external/` in tmc-app) is Rust and has no
local IPC, and this CLI is stdlib-only Python, so the CLI cannot link it. It is
a port: `src/tmc_cli/external/parse.py` follows `parse.rs` case for case,
`resolve.py` the free provider adapters (Modrinth, Thunderstore, GitHub, direct,
Steam). Providers that need a key (CurseForge, mod.io) are reached as a client of
the site, `GET /api/app/v1/external/providers` and `/external/resolve`, through
the stored app-user session (`tmc auth app-login`). The same checks apply as in
the app: provider must match, every file link must be https, a file or a
manual-only page must be present. Signed out, 404 or any error means "off".

## Rules carried over

- A link typed by the user installs directly, no confirmation. `--dry-run`
  resolves and prints the plan without downloading.
- No download cap by default. `--max-mb N` (or `TMC_MODS_MAX_MB`); 0 is unlimited.
  An over-cap failure names `--max-mb`.
- Verification: sha512 (Modrinth), sha256 (GitHub digest, direct `#sha256=`),
  size only (Thunderstore). A mismatch installs nothing.
- No keys in source or output. nxm grants (`key`, `expires`, `user_id`) are
  dropped from anything stored or shown. Nexus needs the user's own key, so it is
  reported as not enabled.
- The CLI never registers or replaces a scheme handler; `link-status` only reads.

## Exit codes

9: keyed provider not enabled here. 10: nothing to download, the link is a page
(Steam, manual-only; `--open` opens it). Others as in the README.

## Where it installs

The app keeps local mods in its own SQLite library, which the CLI does not write.
The CLI installs into `<store>/<provider>/<project>/<version>/` with a manifest
`external-mods.json` (`--dest`, else `TMC_MODS_DIR`, else the user data dir
`tmc/external-mods`). `.zip` files are extracted (zip-slip and symlinks refused);
other files are copied. `remove` only deletes inside the store. The app does not
read this manifest yet. Dependencies are listed, not installed.
