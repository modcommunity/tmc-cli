"""Argument parsing and dispatch.

Two conventions worth stating once:

**Global flags work anywhere.** `tmc --profile ci mod list` and
`tmc mod list --profile ci` are the same command. That is done by attaching the
shared flags to every leaf parser with `default=SUPPRESS`, so a leaf that did
not see the flag leaves the root's value alone instead of overwriting it with
`None` — the classic argparse trap.

**Two names are taken by automation.** `tmc release` is the publish workflow and
`tmc media` is the gallery shortcut, so the free-standing `release` and `media`
content types (which exist, and which the API tells you to prefer the relation
over anyway) live under `tmc content release` and `tmc content media`.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Callable, Sequence

from . import contract, output
from .context import Context
from .commands import (
    auth_cmd,
    content_cmd,
    contract_cmd,
    file_cmd,
    misc_cmd,
    relation_cmd,
    release_cmd,
)
from .errors import ApiError, CliError, EXIT_OK, EXIT_USAGE
from .output import FORMATS
from .schema import (
    ALL_TYPES,
    ANON_APP_FILTER_TYPES,
    ANON_OFFICIAL_FILTER_TYPES,
    ANON_TYPES,
    RELATIONS,
    TYPES,
)
from .version import __version__

# Types that get a top-level command of their own. `release` and `media` are
# deliberately absent — see the module docstring.
TOP_LEVEL_TYPES = tuple(
    name for name in ALL_TYPES if name not in ("release", "media")
)

SUPPRESS = argparse.SUPPRESS


# ---- shared flags ------------------------------------------------------------


def _add_connection_flags(parser: argparse.ArgumentParser, *, leaf: bool) -> None:
    """Credentials and transport. `leaf` copies suppress rather than default."""

    default = SUPPRESS if leaf else None

    group = parser.add_argument_group("connection")

    group.add_argument("--profile", "-P", default=default, help="stored profile to use")
    group.add_argument(
        "--anon",
        action="store_true",
        default=default,
        help=(
            "send no credential at all — the API's public read surface "
            "(summaries of completely public items; GET only)"
        ),
    )
    group.add_argument(
        "--base-url", default=default, help="API root, e.g. https://api.moddingcommunity.com"
    )
    group.add_argument("--token", default=default, help="bearer token (overrides the profile)")
    group.add_argument("--key-id", default=default, help="JWT key id (tmcak_…)")
    group.add_argument("--private-key", default=default, help="path to the Ed25519 PKCS#8 PEM")
    group.add_argument(
        "--jwt-lifetime",
        type=int,
        default=default,
        help="assertion lifetime in seconds (max 300, default 60)",
    )
    group.add_argument("--timeout", type=float, default=default, help="per-request timeout in seconds")
    group.add_argument("--retries", type=int, default=default, help="retry attempts for 429/5xx/network")
    group.add_argument(
        "--retry-wait-max",
        type=float,
        default=default,
        help="longest rate-limit wait to sit through before giving up",
    )
    group.add_argument(
        "--insecure",
        action="store_true",
        default=default,
        help="skip TLS verification (local dev sites with self-signed certs)",
    )
    group.add_argument("--debug", action="store_true", default=default, help="log requests to stderr")
    group.add_argument(
        "--dry-run",
        action="store_true",
        default=default,
        help="print what would be sent and send nothing",
    )


def _add_output_flags(parser: argparse.ArgumentParser, *, leaf: bool) -> None:
    default = SUPPRESS if leaf else None

    group = parser.add_argument_group("output")

    group.add_argument(
        "-o",
        "--output",
        choices=FORMATS,
        default="table" if not leaf else SUPPRESS,
        help="output format (default: table)",
    )
    group.add_argument(
        "--field",
        action="append",
        default=default,
        help="keep only these fields (repeatable, or comma-separated)",
    )
    group.add_argument("-q", "--quiet", action="store_true", default=default, help="suppress progress on stderr")
    group.add_argument("-y", "--yes", action="store_true", default=default, help="assume yes for confirmations")


def _leaf(
    subparsers: Any, name: str, help_: str, handler: Callable[[Context], int], **kwargs: Any
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(name, help=help_, description=help_, **kwargs)

    _add_connection_flags(parser, leaf=True)
    _add_output_flags(parser, leaf=True)
    parser.set_defaults(handler=handler)

    return parser


def _add_body_flags(parser: argparse.ArgumentParser, spec: Any) -> None:
    """The flags that shape a create/update payload."""

    group = parser.add_argument_group("payload")

    group.add_argument(
        "--set",
        action="append",
        metavar="FIELD=VALUE",
        help="set a field; the value is coerced to the field's type",
    )
    group.add_argument(
        "--set-json",
        action="append",
        metavar="FIELD=JSON",
        help="set a field from a JSON literal",
    )
    group.add_argument(
        "--set-file",
        action="append",
        metavar="FIELD=PATH",
        help="set a field from a file's contents ('-' for stdin)",
    )
    group.add_argument(
        "--json",
        metavar="JSON|@FILE|-",
        help="whole body as JSON; --set flags are applied on top",
    )
    group.add_argument(
        "--content-file",
        metavar="PATH",
        help="shorthand for --set-file content=PATH",
    )
    group.add_argument(
        "--tag",
        action="append",
        metavar="NAME",
        help="tag name (repeatable). Replaces the whole tag set — use 'tmc tags add' to append",
    )
    group.add_argument(
        "--from-file",
        metavar="PATH",
        help="a JSON array of items; batched into the API's 25-per-request cap",
    )
    group.add_argument(
        "--allow-unknown-fields",
        action="store_true",
        help="send fields this CLI does not know about (the server still validates)",
    )

    for flag in spec.image_fields:
        group.add_argument(
            f"--{flag}",
            metavar="PATH|FILEID",
            help=f"{flag} image: a local path is uploaded first, anything else is a file id",
        )


def _add_list_filters(
    parser: argparse.ArgumentParser, type_name: str, spec: Any
) -> None:
    """Paging and filtering, for anything that returns a page of rows.

    Attached to BOTH `list` and `get`, because `tmc mod get` with no id is the
    listing — a flag that works under one spelling and not the other is worse
    than not offering the spelling at all.
    """

    parser.add_argument("--page", type=int, default=1, help="page number (default 1)")
    parser.add_argument("--limit", type=int, help="page size (server default 1000)")
    parser.add_argument(
        "--mine",
        action="store_true",
        help="only rows you own — the only way to list your own hidden items",
    )
    parser.add_argument("--all", action="store_true", help="walk every page")
    parser.add_argument("--max", type=int, help="stop after this many rows (with --all)")

    if "search" in spec.list_filters:
        parser.add_argument("--search", help="name/title contains (case-insensitive)")
    else:
        parser.add_argument("--search", help=argparse.SUPPRESS)

    parser.add_argument("--tag", action="append", help="has any of these tags (repeatable)")
    parser.add_argument("--category", action="append", type=int, help="category id (repeatable)")
    parser.add_argument("--community", type=int, help="community id")
    parser.add_argument(
        "--nsfw", action=argparse.BooleanOptionalAction, default=None, help="filter on the NSFW flag"
    )

    # `appId` is the ONE filter the anonymous listing implements, and the keyed
    # one implements every other filter but not this. They are two endpoints, so
    # the flag is offered only where it does something.
    if type_name in ANON_APP_FILTER_TYPES:
        parser.add_argument(
            "--app",
            type=int,
            metavar="ID",
            help="app id — anonymous listings only (--anon); the keyed list has no app filter",
        )
    else:
        parser.add_argument("--app", type=int, help=argparse.SUPPRESS)

    # `official` is the other anonymous-only filter, and for articles it IS the
    # blog — the flag is what puts a post there. Without it a caller wanting the
    # blog has to page the whole article table and filter client-side.
    if type_name in ANON_OFFICIAL_FILTER_TYPES:
        parser.add_argument(
            "--official",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="only the site's own posts — anonymous listings only (--anon)",
        )
    else:
        parser.add_argument(
            "--official",
            action=argparse.BooleanOptionalAction,
            default=None,
            help=argparse.SUPPRESS,
        )


def _add_type_ops(parser: argparse.ArgumentParser, type_name: str) -> None:
    """The five verbs, for one content type."""

    spec = TYPES[type_name]
    ops = parser.add_subparsers(dest="op", metavar="<operation>", required=True)

    # -- list
    listing = _leaf(ops, "list", f"list {type_name}s", content_cmd.list_items, aliases=["ls"])
    _add_list_filters(listing, type_name, spec)

    # -- get
    read_help = f"read one {type_name}"

    if type_name in ANON_TYPES:
        read_help += " (works without a key: --anon)"

    getting = _leaf(ops, "get", read_help + "; omit the id to list them", content_cmd.get_item)
    getting.add_argument("id", type=int, nargs="?", help="omit to list instead")
    _add_list_filters(getting, type_name, spec)

    # -- create
    creating = _leaf(
        ops,
        "create",
        f"create a {type_name}" + (f" (requires {', '.join(spec.required)})" if spec.required else ""),
        content_cmd.create_item,
    )
    _add_body_flags(creating, spec)

    # -- update
    updating = _leaf(ops, "update", f"update a {type_name} (partial)", content_cmd.update_item)
    updating.add_argument("id", type=int, nargs="?", help="omit only when using --from-file")
    _add_body_flags(updating, spec)

    # -- delete
    deleting = _leaf(ops, "delete", f"delete {type_name}s", content_cmd.delete_items)
    deleting.add_argument("ids", type=int, nargs="+")

    parser.set_defaults(type=type_name)


# ---- parser ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tmc",
        description=(
            "Command-line client for TMC's content API "
            "(https://…/api/content). Supports both bearer tokens and "
            "Ed25519 signed assertions."
        ),
        epilog=(
            "Examples:\n"
            "  tmc auth login --token tmc_…\n"
            "  tmc mod list --mine --all -o json\n"
            "  tmc mod create --set name='My Mod' --set appId=1 --content-file README.md\n"
            "  tmc release publish --mod 5 --version 1.2.0 --file 'dist/*.zip'\n"
            "  tmc media add mod 5 --file shot.png --title Screenshot\n"
            "  tmc tags add mod 5 pvp vanilla\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--version", action="version", version=f"tmc {__version__}")

    _add_connection_flags(parser, leaf=False)
    _add_output_flags(parser, leaf=False)

    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    _build_auth(sub)
    _build_contract(sub)
    _build_types(sub)
    _build_content(sub)
    _build_relations(sub)
    _build_files(sub)
    _build_releases(sub)
    _build_misc(sub)

    return parser


def _build_auth(sub: Any) -> None:
    auth = sub.add_parser(
        "auth",
        help="log in, switch profiles, inspect the current key",
        description="Credentials are stored in ~/.config/tmc/config.json (mode 0600).",
    )
    ops = auth.add_subparsers(dest="op", metavar="<operation>", required=True)

    # --token / --key-id / --private-key / --base-url / --profile come from the
    # shared connection group: on `login` they name what to STORE rather than
    # what to override, which is the same information under the same names.
    login = _leaf(
        ops,
        "login",
        "store a credential in a profile",
        auth_cmd.login,
        epilog=(
            "Bearer:  tmc auth login --token tmc_…            (prompted for if omitted)\n"
            "JWT:     tmc auth login --jwt --key-id tmcak_… --private-key key.pem\n"
            "--profile names the profile to write; --base-url the site it belongs to."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    login.add_argument("--jwt", action="store_true", help="JWT (Ed25519) mode")
    login.add_argument(
        "--copy-key",
        action="store_true",
        help="copy the PEM into the config dir (mode 0600) instead of referencing it",
    )
    login.add_argument("--set-default", action="store_true", help="make this the default profile")
    login.add_argument("--no-verify", action="store_true", help="skip the verification request")

    _leaf(ops, "list", "list stored profiles", auth_cmd.list_profiles)

    use = _leaf(ops, "use", "set the default profile", auth_cmd.use_profile)
    use.add_argument("name")

    remove = _leaf(ops, "remove", "delete a stored profile", auth_cmd.remove_profile)
    remove.add_argument("name")

    who = _leaf(
        ops,
        "whoami",
        "show the active credential and what the server lets it do",
        auth_cmd.whoami,
    )
    who.add_argument(
        "--read-only",
        action="store_true",
        help="probe only GET (skips the two harmless write/delete probes)",
    )

    token = _leaf(
        ops,
        "token",
        "print an Authorization value (a JWT key mints a fresh single-use assertion)",
        auth_cmd.show_token,
    )
    token.add_argument("--header", action="store_true", help="print the full header line")

    _leaf(ops, "doctor", "check crypto backend, config permissions and connectivity", auth_cmd.doctor)


def _build_contract(sub: Any) -> None:
    """`tmc contract` — the site's own answer about what exists.

    Separate from `schema`, which DESCRIBES the types: this manages where that
    description comes from. Keeping them apart means `tmc schema mod` reads the
    same whether or not a contract has ever been fetched, and the fetching is
    something you go and do rather than something `schema` does behind your back.
    """

    contract = sub.add_parser(
        "contract",
        help="sync this CLI's idea of the API with the site's own",
        description=(
            "The site publishes its content registry and the command grammar of "
            "its web console at <base>/api/content/spec, with no key. Syncing it "
            "makes --set coercion, the unknown-field check, 'tmc schema' and "
            "completion answer from the live site instead of this build's mirror."
        ),
    )
    ops = contract.add_subparsers(dest="op", metavar="<operation>", required=True)

    _leaf(ops, "sync", "fetch the site's contract and cache it", contract_cmd.sync)
    _leaf(ops, "show", "what is cached, and whether it is in use", contract_cmd.show)
    _leaf(ops, "clear", "forget the cached contract", contract_cmd.clear)

    drift = _leaf(
        ops,
        "drift",
        "what the site has that this CLI does not, and the other way round",
        contract_cmd.drift,
        epilog=(
            "Exits 3 when the two disagree, so CI can run it.\n"
            "Fields are already corrected by a synced contract; a missing COMMAND "
            "needs a release of this CLI."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    drift.add_argument(
        "--fetch",
        action="store_true",
        help="fetch fresh instead of reading the cache, and do not write it",
    )


def _build_types(sub: Any) -> None:
    for name in TOP_LEVEL_TYPES:
        spec = TYPES[name]

        parser = sub.add_parser(
            name,
            help=f"manage {name}s",
            description=spec.note or f"CRUD for {name} items.",
        )

        _add_type_ops(parser, name)


def _build_content(sub: Any) -> None:
    """`tmc content <type> <op>` — uniform access to every type, including the
    two whose names the automation commands took."""

    content = sub.add_parser(
        "content",
        help="CRUD for any content type, including 'release' and 'media'",
        description=(
            "The generic form. 'tmc content mod list' is the same command as "
            "'tmc mod list'; the free-standing 'release' and 'media' types are "
            "only reachable here."
        ),
    )
    types = content.add_subparsers(dest="type", metavar="<type>", required=True)

    for name in ALL_TYPES:
        spec = TYPES[name]
        parser = types.add_parser(name, help=spec.note or f"CRUD for {name}")

        _add_type_ops(parser, name)


def _build_relations(sub: Any) -> None:
    relation_parents = sorted({parent for rel in RELATIONS.values() for parent in rel.parents})

    rel = sub.add_parser(
        "rel",
        help="read and write relations (tags, media, releases, links, sources, items)",
        description=(
            "add = POST (merge, leaves the rest alone); set = PUT (this is now "
            "the complete set); rm = DELETE the named members; clear = DELETE all."
        ),
    )
    ops = rel.add_subparsers(dest="op", metavar="<operation>", required=True)

    def add_target(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("type", choices=relation_parents)
        parser.add_argument("id", type=int)
        parser.add_argument("relation", choices=sorted(RELATIONS))

    getting = _leaf(ops, "get", "read a relation's current set", relation_cmd.rel_get)
    add_target(getting)

    adding = _leaf(ops, "add", "merge members in, keeping the rest (POST)", relation_cmd.rel_add)
    add_target(adding)
    adding.add_argument("members", nargs="*", help="tag names, or JSON objects")
    adding.add_argument("--member", action="append", help="one member as JSON (repeatable)")
    adding.add_argument("--from-file", metavar="PATH", help="a JSON array of members")

    setting = _leaf(
        ops, "set", "replace the whole set (PUT) — omitted members are deleted", relation_cmd.rel_set
    )
    add_target(setting)
    setting.add_argument("members", nargs="*", help="tag names, or JSON objects")
    setting.add_argument("--member", action="append", help="one member as JSON (repeatable)")
    setting.add_argument("--from-file", metavar="PATH", help="a JSON array of members")

    removing = _leaf(ops, "rm", "remove named members", relation_cmd.rel_rm)
    add_target(removing)
    removing.add_argument("keys", nargs="+", help="row ids, or tag names")

    clearing = _leaf(ops, "clear", "remove every member", relation_cmd.rel_clear)
    add_target(clearing)

    # -- tags shortcut
    tags = sub.add_parser("tags", help="tag shortcuts (a tag member is a plain string)")
    tag_ops = tags.add_subparsers(dest="op", metavar="<operation>", required=True)

    tag_parents = sorted(RELATIONS["tags"].parents)

    listing = _leaf(tag_ops, "list", "list an item's tags", relation_cmd.tags_list)
    listing.add_argument("type", choices=tag_parents)
    listing.add_argument("id", type=int)

    for verb, help_, handler in (
        ("add", "add tags, keeping the existing ones", relation_cmd.tags_add),
        ("set", "replace the whole tag set", relation_cmd.tags_set),
        ("rm", "remove tags", relation_cmd.tags_rm),
    ):
        parser = _leaf(tag_ops, verb, help_, handler)
        parser.add_argument("type", choices=tag_parents)
        parser.add_argument("id", type=int)
        parser.add_argument("names", nargs="+")

    # -- media shortcut
    media = sub.add_parser(
        "media",
        help="gallery shortcuts (--file uploads and attaches in one step)",
        description=(
            "The gallery relation. A member is either an uploaded file or an "
            "external URL; --file does the upload for you and attaches the id."
        ),
    )
    media_ops = media.add_subparsers(dest="op", metavar="<operation>", required=True)
    media_parents = sorted(RELATIONS["media"].parents)

    listing = _leaf(media_ops, "list", "list an item's gallery", relation_cmd.media_list)
    listing.add_argument("type", choices=media_parents)
    listing.add_argument("id", type=int)

    adding = _leaf(media_ops, "add", "add gallery entries", relation_cmd.media_add)
    adding.add_argument("type", choices=media_parents)
    adding.add_argument("id", type=int)
    adding.add_argument("--file", action="append", metavar="PATH", help="upload and attach (repeatable, globs ok)")
    adding.add_argument("--url", action="append", metavar="URL", help="attach an external URL (repeatable)")
    adding.add_argument("--title", help="title (only applied when adding exactly one entry)")
    adding.add_argument("--description", help="description (single entry only)")
    adding.add_argument("--type", dest="type_", choices=("IMAGE", "VIDEO"), help="media type")
    adding.add_argument("--member", action="append", help="one member as JSON (repeatable)")
    adding.add_argument("--from-file", metavar="PATH", help="a JSON array of members")

    removing = _leaf(media_ops, "rm", "remove gallery entries by id", relation_cmd.media_rm)
    removing.add_argument("type", choices=media_parents)
    removing.add_argument("id", type=int)
    removing.add_argument("ids", nargs="+")

    # -- links shortcut
    links = sub.add_parser("links", help="link shortcuts")
    link_ops = links.add_subparsers(dest="op", metavar="<operation>", required=True)
    link_parents = sorted(RELATIONS["links"].parents)

    listing = _leaf(link_ops, "list", "list an item's links", relation_cmd.links_list)
    listing.add_argument("type", choices=link_parents)
    listing.add_argument("id", type=int)

    adding = _leaf(link_ops, "add", "add links", relation_cmd.links_add)
    adding.add_argument("type", choices=link_parents)
    adding.add_argument("id", type=int)
    adding.add_argument("urls", nargs="+")
    adding.add_argument(
        "--type",
        dest="link_type",
        choices=("WEBSITE", "X", "FACEBOOK", "STEAM", "DISCORD", "GITHUB", "INSTAGRAM", "YOUTUBE"),
        help="link type; the server canonicalises the URL against it",
    )

    removing = _leaf(link_ops, "rm", "remove links by id", relation_cmd.links_rm)
    removing.add_argument("type", choices=link_parents)
    removing.add_argument("id", type=int)
    removing.add_argument("ids", nargs="+")


def _build_files(sub: Any) -> None:
    files = sub.add_parser(
        "file",
        help="upload and manage FileUploads (what releases and media point at)",
        description=(
            "Uploads are capped at 20 files per request (batched here "
            "automatically) and at the key owner's per-file size limit — "
            "20 MB standard, 1 GB for supporters."
        ),
    )
    ops = files.add_subparsers(dest="op", metavar="<operation>", required=True)

    upload = _leaf(ops, "upload", "upload one or more files", file_cmd.upload)
    upload.add_argument("paths", nargs="+", help="paths or globs")
    upload.add_argument("--title", help="title (single-file uploads only)")
    upload.add_argument("--description", help="description (single-file uploads only)")
    upload.add_argument(
        "--release-id",
        type=int,
        help="attach to this release as it uploads (needs write access to its parent item)",
    )
    upload.add_argument(
        "--raw",
        action="store_true",
        help="send the bytes as the body instead of multipart (one file, answers with one object)",
    )
    upload.add_argument("--name", help="filename to record, with --raw")
    upload.add_argument("--content-type", help="content type, with --raw")

    getting = _leaf(ops, "get", "file metadata and CDN URL", file_cmd.get)
    getting.add_argument("id")

    updating = _leaf(ops, "update", "change a file's title/description", file_cmd.update)
    updating.add_argument("id")
    updating.add_argument("--title")
    updating.add_argument("--description")

    removing = _leaf(ops, "rm", "delete files (and their S3 objects)", file_cmd.remove)
    removing.add_argument("ids", nargs="+")

    download = _leaf(ops, "download", "download a file via its CDN URL", file_cmd.download)
    download.add_argument("id")
    download.add_argument("-O", "--output-path", help="destination path or directory")
    download.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="replace an existing file when the name comes from the file's title",
    )


def _build_releases(sub: Any) -> None:
    releases = sub.add_parser(
        "release",
        help="publish releases with their files attached (the free-standing type is 'tmc content release')",
        description=(
            "Upload, attach and publish in one command. Uses POST on the "
            "releases relation, so other releases on the item are never touched."
        ),
    )
    ops = releases.add_subparsers(dest="op", metavar="<operation>", required=True)

    def add_parent(parser: argparse.ArgumentParser) -> None:
        group = parser.add_argument_group("parent item (exactly one)")
        group.add_argument("--mod", type=int, metavar="ID")
        group.add_argument("--asset", type=int, metavar="ID")
        group.add_argument("--server", type=int, metavar="ID")

    publish = _leaf(
        ops, "publish", "create or update a release, uploading its files", release_cmd.publish
    )
    add_parent(publish)
    publish.add_argument("--version", help="version string; matches an existing release by exact value")
    publish.add_argument("--release-id", type=int, help="update this release by id instead of by version")
    publish.add_argument("--title")
    publish.add_argument("--description")
    publish.add_argument("--content", help="release notes (markdown)")
    publish.add_argument("--content-file", metavar="PATH", help="release notes from a file")
    publish.add_argument(
        "--file", action="append", metavar="PATH", help="file to upload and attach (repeatable, globs ok)"
    )
    publish.add_argument(
        "--file-id", action="append", metavar="ID", help="attach an already-uploaded file (repeatable)"
    )
    publish.add_argument(
        "--replace-files",
        action="store_true",
        help="make the attached set exactly these files (default: merge with what's there)",
    )
    publish.add_argument(
        "--hidden",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="publish as a draft (default: keep the current value, or false on create)",
    )

    listing = _leaf(ops, "list", "list an item's releases", release_cmd.list_releases)
    add_parent(listing)

    removing = _leaf(ops, "rm", "delete a release", release_cmd.remove)
    add_parent(removing)
    removing.add_argument("--version")
    removing.add_argument("--release-id", type=int)

    files = _leaf(ops, "files", "list the files attached to a release", release_cmd.files)
    add_parent(files)
    files.add_argument("--version")
    files.add_argument("--release-id", type=int)


def _build_misc(sub: Any) -> None:
    schema = _leaf(sub, "schema", "show the types, fields and relations this API exposes", misc_cmd.schema)
    schema.add_argument("type", nargs="?", choices=sorted(TYPES), help="one type's fields")

    template = _leaf(sub, "template", "print a starter JSON array for a relation", misc_cmd.template)
    template.add_argument("relation", choices=sorted(RELATIONS))

    raw = _leaf(sub, "raw", "send an arbitrary authenticated request", misc_cmd.raw)
    raw.add_argument("method", help="GET, POST, PUT, DELETE …")
    raw.add_argument("path", help="e.g. /api/content/mod/5/releases")
    raw.add_argument("--param", action="append", metavar="K=V", help="query parameter (repeatable)")
    raw.add_argument("--json", metavar="JSON|@FILE|-", help="request body")
    raw.add_argument("--set", action="append", metavar="FIELD=VALUE")
    raw.add_argument("--set-json", action="append", metavar="FIELD=JSON")
    raw.add_argument("--set-file", action="append", metavar="FIELD=PATH")
    raw.add_argument(
        "--envelope", action="store_true", help="print the whole response, not just its 'data'"
    )

    opening = _leaf(
        sub,
        "open",
        "print (or open) an item's page on the site",
        misc_cmd.open_item,
        epilog=(
            "The address is read off the item, not built from its id: a mod "
            "lives under its app, so the app segment and the slug both come "
            "from the record."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    opening.add_argument("type", choices=sorted(TYPES), help="content type")
    opening.add_argument("id", type=int)
    opening.add_argument(
        "--browser", action="store_true", help="open it in a browser as well as printing it"
    )

    completion = _leaf(sub, "completion", "print a shell completion script", misc_cmd.completion)
    completion.add_argument("shell", choices=("bash", "zsh", "fish"))


def local_command_paths() -> set[tuple[str, ...]]:
    """Every command this build understands, as ('mod', 'list') tuples.

    Walked off the BUILT PARSER rather than listed by hand, so a command added
    to `_build_*` is a command `tmc contract drift` compares. A hand-kept list
    would be a third mirror, and this whole feature exists because mirrors rot.
    """

    out: set[tuple[str, ...]] = set()

    def walk(parser: argparse.ArgumentParser, prefix: tuple[str, ...]) -> None:
        subs = [
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]

        if not subs:
            if prefix:
                out.add(prefix)

            return

        for action in subs:
            for name, child in action.choices.items():
                walk(child, prefix + (name,))

    walk(build_parser(), ())

    return out


def top_level_commands() -> list[str]:
    """Command names, for the completion scripts."""

    return sorted(
        list(TOP_LEVEL_TYPES)
        + ["auth", "contract", "content", "rel", "tags", "media", "links", "file", "release", "schema", "template", "raw", "open", "completion"]
    )


# ---- entry point -------------------------------------------------------------


def _normalise_fields(args: argparse.Namespace) -> None:
    """`--field id,name --field url` → ['id', 'name', 'url']."""

    raw = getattr(args, "field", None)

    if not raw:
        args.field = None
        return

    names: list[str] = []

    for entry in raw:
        names.extend(part.strip() for part in entry.split(",") if part.strip())

    args.field = names


def rewrite_trailing_help(parser: argparse.ArgumentParser, argv: list[str]) -> list[str]:
    """`tmc mod help` → `tmc mod --help`.

    argparse gives every sub-parser `-h/--help` and no `help` sub-command, so the
    word people type out of git/docker habit came back as
    `invalid choice: 'help'`. The web console accepts both spellings; so does
    this.

    Only rewritten when every word BEFORE it is a command — `tmc tags add mod 5
    help` adds a tag called "help" and has to keep doing so. That also means a
    global flag in front of the command (`tmc --profile ci mod help`) is not
    rewritten: the walk cannot know a flag's arity, and guessing would eat a
    value. `--help` still works there, as it does everywhere.
    """

    if not argv or argv[-1] != "help":
        return argv

    node = parser
    depth = 0

    for word in argv[:-1]:
        subs = [
            action
            for action in node._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        child = next(
            (action.choices[word] for action in subs if word in action.choices), None
        )

        if child is None:
            break

        node = child
        depth += 1

    if depth != len(argv) - 1:
        return argv

    return argv[:-1] + ["--help"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()

    raw = list(argv) if argv is not None else sys.argv[1:]

    try:
        args = parser.parse_args(rewrite_trailing_help(parser, raw))
    except SystemExit as err:  # argparse already printed the message
        # `err.code or EXIT_USAGE` turned argparse's clean 0 — what `--help`
        # and `--version` exit with — into a usage error, so `tmc --help` has
        # always reported failure to a shell.
        return EXIT_USAGE if err.code is None else int(err.code)

    _normalise_fields(args)

    handler: Callable[[Context], int] | None = getattr(args, "handler", None)

    if handler is None:
        parser.print_help()
        return EXIT_USAGE

    ctx = Context(args)

    """
    Apply the cached contract, if there is a usable one for this profile.

    Best effort and silent. It is an ACCURACY improvement — it corrects the
    field list this build guessed at — and a CLI that refused to run because it
    could not read a cache file would have traded a small inaccuracy for a total
    outage. `tmc contract show` is where somebody asks whether it is in use.

    Skipped for `tmc contract` itself: those commands reason about the cache and
    must see the build's own mirror, not a view already corrected by it.
    """
    if getattr(args, "command", None) != "contract":
        try:
            contract.install_if_usable(ctx.settings.base_url)
        except Exception:
            pass

    try:
        return handler(ctx)
    except ApiError as err:
        output.error(err.format())

        if err.hint:
            output.error(f"  → {err.hint}")

        return err.exit_code
    except CliError as err:
        output.error(f"error: {err.message}")

        if err.hint:
            output.error(f"  → {err.hint}")

        return err.exit_code
    except BrokenPipeError:
        # `tmc mod list | head` closes the pipe under us. That is not a failure,
        # but Python's shutdown would print a scary traceback about it.
        try:
            sys.stdout.close()
        finally:
            return EXIT_OK
    except KeyboardInterrupt:
        output.error("Interrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
