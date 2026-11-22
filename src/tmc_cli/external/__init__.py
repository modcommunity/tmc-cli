"""Mods from sites other than TMC: parse a link, resolve it, install it.

A Python port of the app's `src-tauri/core/src/external/` (tmc-app, branch
`nightly/external-creds`, docs/external-mods.md). The CLI cannot link that Rust
core, and the app exposes no local IPC a second process could call (its IPC is
Tauri's, in-process), so the rules are re-implemented here and the two must
agree. `parse.py` mirrors `parse.rs` case for case; the keyed providers go
through the same site endpoints the app uses (`/api/app/v1/external/*`).
"""
