"""A local mirror of the API's content registry.

WHY MIRROR IT AT ALL
--------------------
The API is strict: every create/update schema is `.strict()`, so a misspelt
field is a `400` rather than a silent no-op. That is the right server behaviour
and a poor CLI one — a round trip to be told `tgs` is not `tags` is a round trip
that did not have to happen. Knowing the field list locally lets the CLI:

  - reject a typo before spending a request (and suggest the near-miss),
  - coerce `--set hidden=true` to a real boolean, `--set appId=3` to a number,
    since the schemas do not coerce and `"3"` is a validation error,
  - answer `tmc schema mod` without asking the server anything,
  - complete field names in the shell.

WHAT IT IS NOT
--------------
It is not authority. The server decides; this only decides what is worth
sending. Anything unknown here can still be forced through with `--set-raw` /
`--json`, so a field added upstream is never blocked by a stale mirror — it
just loses the local check until this file catches up.

Mirrored from `src/lib/api/public/content.ts` and the `Create*Input` schemas it
derives from (`src/types/<type>/post.ts`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---- Field types -------------------------------------------------------------

STR = "str"
INT = "int"
FLOAT = "float"
BOOL = "bool"
DATE = "date"
JSON = "json"
STR_LIST = "str[]"
INT_LIST = "int[]"

#: Enumerations the server pins. Offered for completion and checked locally so
#: a wrong casing (`mit` for `MIT`) is caught before the request.
ENUMS: dict[str, tuple[str, ...]] = {
    "environment": ("ALL", "SERVER", "CLIENT"),
    "license": (
        "MIT",
        "APACHE_2_0",
        "BSD_3_CLAUSE",
        "GPL",
        "CC0",
        "CC_BY",
        "CC_BY_NC",
        "ALL_RIGHTS_RESERVED",
    ),
    "mediaType": ("IMAGE", "VIDEO"),
    "linkType": (
        "WEBSITE",
        "X",
        "FACEBOOK",
        "STEAM",
        "DISCORD",
        "GITHUB",
        "INSTAGRAM",
        "YOUTUBE",
    ),
    "os": ("WINDOWS", "LINUX", "MAC"),
}


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    note: str = ""
    #: Set on fields the server accepts but strips or refuses for a normal key.
    staff_only: bool = False
    enum: str | None = None


def f(name: str, type_: str, note: str = "", **kwargs: Any) -> Field:
    return Field(name, type_, note, **kwargs)


# ---- Shared groups -----------------------------------------------------------

# The polymorphic "which content item does this row hang off" columns shared by
# favorite / filter / comment / review / media.
TARGET_FKS = [
    f("appId", INT),
    f("assetId", INT),
    f("modId", INT),
    f("serverId", INT),
    f("communityId", INT),
    f("articleId", INT),
    f("collectionId", INT),
    f("serverMapId", INT),
]

ENGAGEMENT = [
    f("allowRatings", BOOL),
    f("allowReviews", BOOL),
    f("allowComments", BOOL),
    f("allowMedia", BOOL),
]

IMAGES = [
    f("iconId", STR, "FileUpload id — you must own the file"),
    f("bannerId", STR, "FileUpload id — you must own the file"),
    f("cardId", STR, "FileUpload id — you must own the file"),
]

# Two switches every canonical type carries EXCEPT server — `serverFields` is an
# explicit `ServerInput.pick()` rather than an omit-list (so the scanner's
# telemetry can never leak onto the write surface by being added upstream), and
# it does not pick either of these even though `ServerInput` defines them.
VISIBILITY = [
    f(
        "delist",
        BOOL,
        "drop from listings, carousels, search and the sitemap; the page, its "
        "address and everything attached to it stay",
    ),
    f("usesAi", BOOL, "self-declared AI disclosure — gates and filters nothing"),
]

# Moderator-only overrides. Present on the wire (the schemas carry them) but
# resolved server-side against the caller's role, so an ordinary key setting one
# is refused rather than obeyed.
OWNERSHIP = [
    f("createdAt", DATE, "backdate creation", staff_only=True),
    f("lastEdit", DATE, "explicit last-edited stamp", staff_only=True),
]


@dataclass(frozen=True)
class TypeSpec:
    """One content type as the API exposes it."""

    name: str
    #: Canonical types run through the site's own writers and own relations.
    canonical: bool
    #: Required by the CREATE schema specifically (update is always partial).
    required: tuple[str, ...]
    fields: tuple[Field, ...]
    relations: tuple[str, ...] = ()
    list_filters: tuple[str, ...] = ()
    #: Default table columns, chosen to fit a terminal rather than be complete.
    columns: tuple[str, ...] = ("id", "name", "hidden", "createdAt")
    #: CLI-name → payload-field for the three image slots. Servers spell theirs
    #: without the `Id` suffix, which is the one place the dialects differ.
    image_fields: dict[str, str] = field(default_factory=dict)
    staff_only_write: bool = False
    note: str = ""

    def field_names(self) -> list[str]:
        return [item.name for item in self.fields]

    def get(self, name: str) -> Field | None:
        for item in self.fields:
            if item.name == name:
                return item

        return None


_IMAGE_ID_FIELDS = {"icon": "iconId", "banner": "bannerId", "card": "cardId"}
_IMAGE_BARE_FIELDS = {"icon": "icon", "banner": "banner", "card": "card"}


TYPES: dict[str, TypeSpec] = {
    "asset": TypeSpec(
        name="asset",
        canonical=True,
        required=("name",),
        fields=tuple(
            [
                f("name", STR),
                f("url", STR, "slug; changing it leaves a redirect behind"),
                f("description", STR, "short plain-text summary"),
                f("content", STR, "the body (markdown)"),
                f("virusScanLink", STR, "link to a third-party scan report"),
                f("environment", STR, enum="environment"),
                f("license", STR, enum="license"),
                f("hidden", BOOL, "draft — invisible to everyone but you"),
                f("nsfw", BOOL),
                f("archived", BOOL),
                f("apiPublic", BOOL, "answer this item without a key (default true)"),
                f("subDisabled", BOOL, "opt out of one-click subscribe/install"),
                f("isOfficial", BOOL, staff_only=True),
                f("appId", INT),
                f("communityId", INT),
                f("categoryId", INT, "legacy single-select"),
                f("categoryIds", INT_LIST, "replaces the whole category set"),
                f("tags", STR_LIST, "replaces the whole tag set"),
                f("media", JSON, "replaces the gallery — prefer the relation"),
                f("releases", JSON, "replaces all releases — prefer the relation"),
                f("sourceItems", JSON, "replaces external sources ('sources' relation)"),
            ]
            + IMAGES
            + ENGAGEMENT
            + OWNERSHIP
            + VISIBILITY
        ),
        relations=("tags", "media", "releases", "sources"),
        list_filters=("search", "tags", "categoryIds", "communityId", "nsfw"),
        columns=("id", "name", "url", "hidden", "nsfw", "createdAt"),
        image_fields=_IMAGE_ID_FIELDS,
    ),
    "mod": TypeSpec(
        name="mod",
        canonical=True,
        required=("name", "content", "appId"),
        fields=tuple(
            [
                f("name", STR),
                f("url", STR, "slug; changing it leaves a redirect behind"),
                f("description", STR),
                f("content", STR, "the body (markdown)"),
                f("install", STR, "installation instructions"),
                f("virusScanLink", STR),
                f("environment", STR, enum="environment"),
                f("license", STR, enum="license"),
                f("hidden", BOOL),
                f("nsfw", BOOL),
                f("archived", BOOL),
                f("apiPublic", BOOL, "answer this item without a key (default true)"),
                f("subDisabled", BOOL, "opt out of one-click subscribe/install"),
                f("isOfficial", BOOL, staff_only=True),
                f("appId", INT, "required on create"),
                f("communityId", INT),
                f("categoryId", INT),
                f("categoryIds", INT_LIST),
                f("tags", STR_LIST),
                f("media", JSON, "prefer the relation"),
                f("releases", JSON, "prefer the relation"),
                f("links", JSON, "prefer the relation"),
                f("sourceItems", JSON, "prefer the 'sources' relation"),
                f("redirect", JSON, '{"type":"FULL|PARTIAL","sourceId":N} or null'),
            ]
            + IMAGES
            + ENGAGEMENT
            + OWNERSHIP
            + VISIBILITY
        ),
        relations=("tags", "media", "releases", "links", "sources"),
        list_filters=("search", "tags", "categoryIds", "communityId", "nsfw"),
        columns=("id", "name", "url", "hidden", "nsfw", "createdAt"),
        image_fields=_IMAGE_ID_FIELDS,
    ),
    "server": TypeSpec(
        name="server",
        canonical=True,
        required=("appId",),
        fields=(
            f("appId", INT, "required on create"),
            f("name", STR),
            f("url", STR),
            f("description", STR),
            f("content", STR),
            f("rules", STR),
            f("srcUrl", STR),
            f("hidden", BOOL),
            f("nsfw", BOOL),
            f("archived", BOOL),
            f("showNetInfo", BOOL),
            f("showUsers", BOOL),
            f("showVars", BOOL),
            f("showSlideshow", BOOL),
            f("allowRatings", BOOL),
            f("allowReviews", BOOL),
            f("allowComments", BOOL),
            f("allowMedia", BOOL),
            f("ip4", STR),
            f("ip6", STR),
            f("port", INT),
            f("portQuery", INT),
            f("hostName", STR),
            f("useHostName", BOOL, "connect by hostname rather than address"),
            f("icon", STR, "FileUpload id (servers drop the 'Id' suffix)"),
            f("banner", STR, "FileUpload id"),
            f("card", STR, "FileUpload id"),
            f("communityId", INT),
            f("categoryId", INT),
            f("categoryIds", INT_LIST, "replaces the whole category set"),
            f("countryId", INT, "auto-detected from the address; correctable"),
            f("gameMode", STR),
            f("version", STR),
            f("password", BOOL, "server is password protected"),
            f("secure", BOOL),
            f("os", STR, enum="os"),
            f("dedicated", BOOL),
            f("tags", STR_LIST),
            f("media", JSON, "prefer the relation"),
            f("links", JSON, "prefer the relation"),
            f("releases", JSON, "prefer the relation"),
            # Measured telemetry. Accepted by the schema, then checked against
            # this server's own unlocks — an integration reporting from the
            # machine, or an owner override. Staff are NOT exempt.
            f("online", BOOL, "gated: needs a stat unlock"),
            f("curUsers", INT, "gated: needs a stat unlock"),
            f("maxUsers", INT, "gated: needs a stat unlock"),
            f("bots", INT, "gated: needs a stat unlock"),
            f("avgUsers", INT, "gated: needs a stat unlock"),
            f("users", JSON, "gated: player list"),
            f("vars", JSON, "gated: key/value rule set"),
            f("map", JSON, "gated: current map"),
        ),
        relations=("tags", "media", "releases", "links"),
        list_filters=("search", "tags", "categoryIds", "communityId", "nsfw"),
        columns=("id", "name", "ip4", "port", "online", "curUsers", "hidden"),
        image_fields=_IMAGE_BARE_FIELDS,
        note=(
            "Telemetry (online/curUsers/…) is measured by the query servers and "
            "needs an unlock. Servers are also the one type with no 'apiPublic' "
            "field — content.ts picks their columns explicitly and leaves it out."
        ),
    ),
    "community": TypeSpec(
        name="community",
        canonical=True,
        required=("name",),
        fields=tuple(
            [
                f("name", STR),
                f("url", STR),
                f("description", STR),
                f("content", STR),
                f("hidden", BOOL),
                f("nsfw", BOOL),
                f("archived", BOOL),
                f("apiPublic", BOOL, "answer this item without a key (default true)"),
                f("ageRequirement", STR, "stated minimum age"),
                f("categoryId", INT),
                f("categoryIds", INT_LIST),
                f("appIds", INT_LIST, "games this community is about"),
                f("tags", STR_LIST),
                f("media", JSON, "prefer the relation"),
                f("links", JSON, "prefer the relation"),
            ]
            + IMAGES
            + ENGAGEMENT
            + OWNERSHIP
            + VISIBILITY
        ),
        relations=("tags", "media", "links"),
        list_filters=("search", "tags", "categoryIds", "nsfw"),
        columns=("id", "name", "url", "hidden", "nsfw", "createdAt"),
        image_fields=_IMAGE_ID_FIELDS,
    ),
    "article": TypeSpec(
        name="article",
        canonical=True,
        required=("title", "content"),
        fields=tuple(
            [
                f("title", STR),
                f("url", STR),
                f("description", STR),
                f("content", STR),
                f("hidden", BOOL),
                f("nsfw", BOOL),
                f("apiPublic", BOOL, "answer this item without a key (default true)"),
                f("categoryId", INT),
                f("categoryIds", INT_LIST),
                f("appId", INT),
                f("communityId", INT),
                f("modId", INT, "article about this mod"),
                f("serverId", INT),
                f("assetId", INT),
                f("userTargetId", STR),
                f("tags", STR_LIST),
                f("media", JSON, "prefer the relation"),
            ]
            + IMAGES
            + ENGAGEMENT
            + OWNERSHIP
            + VISIBILITY
        ),
        relations=("tags", "media"),
        list_filters=("search", "tags", "categoryIds", "communityId", "nsfw"),
        columns=("id", "title", "url", "hidden", "nsfw", "createdAt"),
        image_fields=_IMAGE_ID_FIELDS,
    ),
    "collection": TypeSpec(
        name="collection",
        canonical=True,
        required=("name",),
        fields=tuple(
            [
                f("name", STR),
                f("description", STR),
                f("content", STR),
                f("hidden", BOOL),
                f("nsfw", BOOL),
                f("allowReviews", BOOL),
                f("allowComments", BOOL),
                f("allowMedia", BOOL),
                f("apiPublic", BOOL, "answer this item without a key (default true)"),
                f("subDisabled", BOOL, "opt out of one-click subscribe/install"),
                f("includeItemMedia", BOOL, "merge tied items' galleries"),
                f("categoryIds", INT_LIST),
                f("tags", STR_LIST),
                f("items", JSON, "prefer the 'items' relation"),
                f("createdAt", DATE, staff_only=True),
            ]
            + IMAGES
            + VISIBILITY
        ),
        relations=("tags", "items"),
        list_filters=("search", "tags", "nsfw"),
        columns=("id", "name", "hidden", "nsfw", "createdAt"),
        image_fields=_IMAGE_ID_FIELDS,
    ),
    "group": TypeSpec(
        name="group",
        canonical=True,
        required=("name",),
        fields=tuple(
            [
                f("name", STR),
                f("url", STR),
                f("description", STR),
                f("content", STR),
                f("hidden", BOOL),
                f("nsfw", BOOL),
                f("archived", BOOL),
                f("apiPublic", BOOL, "answer this item without a key (default true)"),
                f("inviteOnly", BOOL),
                f("categoryIds", INT_LIST),
                f("appIds", INT_LIST),
                f("tags", STR_LIST),
                f("appId", INT, "this group IS an app's staff roster"),
                f("articleId", INT),
                f("collectionId", INT),
                f("communityId", INT),
                f("assetId", INT),
                f("modId", INT),
                f("serverId", INT),
                f("serverMapId", INT),
            ]
            + IMAGES
            + ENGAGEMENT
            + OWNERSHIP
            + VISIBILITY
        ),
        # `tags` is the only one: relations.ts lists group under that relation
        # and no other. The sub-resource 404s the rest and says which it has.
        relations=("tags",),
        list_filters=("search", "tags", "categoryIds", "communityId"),
        columns=("id", "name", "url", "hidden", "inviteOnly"),
        image_fields=_IMAGE_ID_FIELDS,
    ),
    "favorite": TypeSpec(
        name="favorite",
        canonical=False,
        required=(),
        fields=tuple(TARGET_FKS),
        columns=("id", "modId", "serverId", "assetId", "articleId"),
    ),
    "filter": TypeSpec(
        name="filter",
        canonical=False,
        required=(),
        fields=tuple(TARGET_FKS),
        columns=("id", "modId", "serverId", "assetId", "articleId"),
    ),
    "comment": TypeSpec(
        name="comment",
        canonical=False,
        required=("content",),
        fields=tuple(
            [f("content", STR), f("parentId", INT, "reply to this comment")]
            + TARGET_FKS
        ),
        columns=("id", "content", "modId", "serverId", "createdAt"),
        note="Subject to the item owner's allowComments toggle.",
    ),
    "review": TypeSpec(
        name="review",
        canonical=False,
        required=(),
        fields=tuple(
            [f("content", STR), f("rating", INT, "0-10")] + TARGET_FKS
        ),
        columns=("id", "rating", "modId", "serverId", "createdAt"),
        note="Subject to the item owner's allowReviews toggle.",
    ),
    "tag": TypeSpec(
        name="tag",
        canonical=False,
        required=("name",),
        fields=(
            f("name", STR),
            f("description", STR),
            f("content", STR),
            f("colorFrom", STR),
            f("colorTo", STR),
            f("hidden", BOOL),
            f("nsfw", BOOL),
        ),
        columns=("id", "name", "hidden"),
        staff_only_write=True,
        note="Staff-only write. Ordinary keys can read the list.",
    ),
    "release": TypeSpec(
        name="release",
        canonical=False,
        required=("version",),
        fields=(
            f("version", STR),
            f("title", STR),
            f("description", STR),
            f("content", STR),
            f("hidden", BOOL),
            f("appId", INT),
            f("modId", INT),
            f("serverId", INT),
            f("assetId", INT),
        ),
        columns=("id", "version", "title", "modId", "assetId", "hidden"),
        note="Prefer 'tmc release publish' or the releases relation — this endpoint cannot attach files.",
    ),
    "media": TypeSpec(
        name="media",
        canonical=False,
        required=(),
        fields=tuple(
            [
                f("type", STR, enum="mediaType"),
                f("title", STR),
                f("description", STR),
                f("content", STR),
                f("externalUrl", STR),
                f("fileId", STR, "FileUpload id — you must own the file"),
                f("hidden", BOOL),
                f("nsfw", BOOL),
            ]
            + TARGET_FKS
        ),
        columns=("id", "type", "title", "externalUrl", "fileId", "modId"),
        note="Prefer the media relation on the parent item.",
    ),
}

CANONICAL_TYPES = tuple(name for name, spec in TYPES.items() if spec.canonical)
ALL_TYPES = tuple(TYPES.keys())


# ---- Relations ---------------------------------------------------------------


@dataclass(frozen=True)
class RelationSpec:
    name: str
    parents: tuple[str, ...]
    #: How a member is identified when merging (POST) or removing (DELETE).
    key: str
    member: str
    columns: tuple[str, ...]


RELATIONS: dict[str, RelationSpec] = {
    "tags": RelationSpec(
        name="tags",
        parents=(
            "asset",
            "mod",
            "server",
            "community",
            "article",
            "collection",
            "group",
        ),
        key="the folded name",
        member="a plain string",
        columns=(),
    ),
    "media": RelationSpec(
        name="media",
        parents=("asset", "mod", "server", "community", "article"),
        key="id",
        member='{ id?, type?: IMAGE|VIDEO, fileId?, externalUrl?, title?, description? }',
        columns=("id", "type", "title", "fileId", "externalUrl"),
    ),
    "releases": RelationSpec(
        name="releases",
        parents=("asset", "mod", "server"),
        key="id",
        member=(
            "{ id?, version, title?, description?, content?, hidden?, "
            "files?: [fileId], archiveJobId? }"
        ),
        columns=("id", "version", "title", "hidden", "files"),
    ),
    "links": RelationSpec(
        name="links",
        parents=("mod", "server", "community"),
        key="id",
        member="{ id?, type?, url }",
        columns=("id", "type", "url"),
    ),
    "sources": RelationSpec(
        name="sources",
        parents=("asset", "mod"),
        key="sourceId",
        member=(
            "{ id?, sourceId, path, externalId?, externalFileId?, "
            "externalAdditional?, isPrimaryGit? }"
        ),
        columns=("id", "sourceId", "path", "externalId"),
    ),
    "items": RelationSpec(
        name="items",
        parents=("collection",),
        key="id",
        member="{ id?, title?, description?, externalUrl?, assetId?, articleId?, modId?, serverId? }",
        columns=("id", "title", "assetId", "modId", "serverId", "externalUrl"),
    ),
}


def relations_for(type_name: str) -> list[str]:
    return [name for name, spec in RELATIONS.items() if type_name in spec.parents]


# ---- The unauthenticated read surface ----------------------------------------
#
# `GET /api/content/<type>[/<id>]` sent with NO `Authorization` header at all is
# answered by a second, much smaller handler (`anon_handler.ts`). It is not the
# keyed surface with the key left off: it returns a public SUMMARY rather than
# the record, it covers the seven content items and nothing else, and it takes
# almost none of the keyed list's filters.
#
# A request that presents a credential never takes that branch — even a bad one,
# which is told it is bad rather than downgraded — so anonymous mode means
# sending no header, not sending an empty one.

#: The types with an anonymous surface (`ANON_TYPES` in `anon.ts`).
ANON_TYPES = (
    "asset",
    "mod",
    "server",
    "community",
    "article",
    "collection",
    "group",
)

#: The two list filters it accepts, each only for the types whose model has the
#: column. Everything else — search, tags, categories, mine — is deliberately
#: absent: the surface exists so an integrator can see the shape of the API, not
#: so it can be used as a search engine.
ANON_LIST_FILTERS = ("appId", "official")

#: Types whose anonymous listing understands `?appId=` (`HAS_APP`).
ANON_APP_FILTER_TYPES = ("asset", "mod", "server", "article")

#: Types whose anonymous listing understands `?official=` (`HAS_OFFICIAL`).
#: Same four types, but a separate table because they are separate sets
#: server-side and only one of them is about apps.
ANON_OFFICIAL_FILTER_TYPES = ("asset", "mod", "server", "article")

#: `api.anon.limitMax`, the ceiling AND the default page size for an anonymous
#: list. An operator can change it, so this is only used to warn.
ANON_LIST_LIMIT_DEFAULT = 20

#: Table columns for a summary. The keyed columns are mostly wrong here — a
#: summary has no `hidden` (it could not be answered if it were) and a server
#: summary keeps its address under `server`, not `ip4`/`port`.
ANON_COLUMNS = ("id", "type", "name", "slug", "path", "createdAt")


def anon_supports(type_name: str) -> bool:
    return type_name in ANON_TYPES


# ---- Limits the server enforces ---------------------------------------------

#: `MAX_BULK_WRITE` in handler.ts — create and bulk-update.
MAX_BULK_WRITE = 25
#: `MAX_BULK` — bulk delete ids.
MAX_BULK_DELETE = 100
#: PUT/POST members. `RELATION_MEMBERS_MAX` bounds the whole body at 500, but
#: the element parse (`z.array(def.schema).max(200)`) is the tighter of the two.
MAX_RELATION_MEMBERS = 200
#: DELETE takes KEYS rather than members, and those only meet `KeyList` — so the
#: 500 body bound is the only one that applies to them.
MAX_RELATION_KEYS = 500
#: `MAX_UPLOAD_PARTS` in file.ts.
MAX_UPLOAD_PARTS = 20
#: Per-file size limits (`UploadSizeLimit`). Only used to warn before uploading;
#: the server has the final say and answers 413.
UPLOAD_LIMIT_USER = 20 * 1024 * 1024
UPLOAD_LIMIT_SUPPORTER = 1024 * 1024 * 1024
