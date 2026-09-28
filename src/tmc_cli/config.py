"""Profiles: where a credential and a base URL live between invocations.

A key is a secret, so the goal is that it is typed once and never again — not
pasted into a shell history on every call. Profiles are stored in
`~/.config/tmc/config.json` with `0600`, and a JWT private key is written beside
it as its own `0600` PEM rather than inlined, so it can be a file the user
already has (`--private-key ~/keys/tmc.pem` is stored as a path, not a copy).

RESOLUTION ORDER
----------------
Explicit flags beat environment variables beat the stored profile. The
environment tier exists for CI, where writing a config file is an extra step and
the secret arrives as `TMC_TOKEN` anyway.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from typing import Any

from .auth import AnonymousCredential, BearerCredential, Credential, JwtCredential
from .ed25519 import KeyError_, SigningKey
from .errors import ConfigError

#: The public APIs moved off the website onto an origin of their own. The apex
#: still answers — nginx PROXIES `/api/content` there rather than redirecting,
#: precisely because a cross-origin redirect strips `Authorization` and would
#: have 401'd every keyed caller — so an existing profile keeps working and
#: nothing has to be migrated. New profiles get the address integrations are
#: told to use.
#:
#: The PATHS keep their `/api` prefix (`/api/content/...`), which is a separate
#: decision from this one. `api.moddingcommunity.com/content/...` is the spelling
#: the docs print, but it exists only because nginx rewrites the prefix back on;
#: `/api/content` is the route's real name in the app and is the one spelling
#: that answers everywhere — on the API origin, on the apex, and against a
#: container or a dev checkout reached directly, where there is no nginx to do
#: the rewriting. A CLI is pointed at all three.
DEFAULT_BASE_URL = "https://api.moddingcommunity.com"

ENV_PREFIX = "TMC_"


def config_dir() -> str:
    base = os.environ.get("TMC_CONFIG_DIR")

    if base:
        return os.path.expanduser(base)

    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )

    return os.path.join(xdg, "tmc")


def open_private(path: str, mode: str = "w"):
    """Create a file only its owner can read, from the moment it exists.

    `open(path, "w")` creates at `0666 & ~umask` — `0644` under the usual umask —
    so a chmod afterwards closes the window only after the secret is already on
    disk under a readable mode. On a shared box that window is a real read. Every
    file this tool writes that holds a credential goes through here instead.

    `O_CREAT`'s mode argument does nothing when the file already exists (a stale
    `.tmp` from an interrupted save), so the fd is chmodded as well.
    """

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)

    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)

        if "b" in mode:
            return os.fdopen(fd, mode)

        return os.fdopen(fd, mode, encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


@dataclass
class Profile:
    """One stored credential plus the site it belongs to."""

    name: str
    base_url: str = DEFAULT_BASE_URL
    auth_mode: str = "bearer"  # bearer | jwt
    token: str | None = None
    key_id: str | None = None
    private_key_path: str | None = None
    jwt_lifetime_sec: int = 60
    #: Optional per-profile defaults, e.g. {"timeout": 60}.
    options: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "base_url": self.base_url,
            "auth_mode": self.auth_mode,
        }

        if self.token:
            out["token"] = self.token

        if self.key_id:
            out["key_id"] = self.key_id

        if self.private_key_path:
            out["private_key_path"] = self.private_key_path

        if self.jwt_lifetime_sec != 60:
            out["jwt_lifetime_sec"] = self.jwt_lifetime_sec

        if self.options:
            out["options"] = self.options

        return out

    @classmethod
    def from_json(cls, name: str, raw: dict[str, Any]) -> "Profile":
        return cls(
            name=name,
            base_url=str(raw.get("base_url") or DEFAULT_BASE_URL),
            auth_mode=str(raw.get("auth_mode") or "bearer"),
            token=raw.get("token"),
            key_id=raw.get("key_id"),
            private_key_path=raw.get("private_key_path"),
            jwt_lifetime_sec=int(raw.get("jwt_lifetime_sec") or 60),
            options=dict(raw.get("options") or {}),
        )

    def credential(self) -> Credential:
        if self.auth_mode == "jwt":
            if not self.key_id:
                raise ConfigError(
                    f"Profile '{self.name}' is JWT mode but has no key id.",
                    hint="Re-run: tmc auth login --jwt --key-id tmcak_… --private-key key.pem",
                )

            if not self.private_key_path:
                raise ConfigError(
                    f"Profile '{self.name}' is JWT mode but has no private key.",
                    hint="Re-run: tmc auth login --jwt --key-id tmcak_… --private-key key.pem",
                )

            try:
                signing_key = SigningKey.from_file(self.private_key_path)
            except KeyError_ as err:
                raise ConfigError(str(err)) from err

            return JwtCredential(self.key_id, signing_key, self.jwt_lifetime_sec)

        if not self.token:
            raise ConfigError(
                f"Profile '{self.name}' has no token.",
                hint="Run: tmc auth login --token tmc_…",
            )

        return BearerCredential(self.token)

    def describe(self) -> str:
        if self.auth_mode == "jwt":
            return f"jwt {self.key_id or '(no key id)'} → {self.private_key_path or '(no key file)'}"

        from .auth import redact

        return f"bearer {redact(self.token)}" if self.token else "bearer (no token)"


@dataclass
class Config:
    default_profile: str = "default"
    profiles: dict[str, Profile] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Config":
        path = config_path()

        if not os.path.exists(path):
            return cls()

        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as err:
            raise ConfigError(f"Cannot read {path}: {err}") from err

        profiles = {
            name: Profile.from_json(name, body)
            for name, body in (raw.get("profiles") or {}).items()
            if isinstance(body, dict)
        }

        return cls(
            default_profile=str(raw.get("default_profile") or "default"),
            profiles=profiles,
        )

    def save(self) -> str:
        path = config_path()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)

        body = {
            "default_profile": self.default_profile,
            "profiles": {
                name: profile.to_json() for name, profile in self.profiles.items()
            },
        }

        # Write through a temp file in the same directory so an interrupted save
        # cannot leave a half-written config. The temp file is created 0600 by
        # `open_private` rather than chmodded after the fact: it holds the same
        # token the final file does, so "never world-readable under its final
        # name" was only half the guarantee worth making.
        tmp = f"{path}.tmp"

        try:
            with open_private(tmp) as handle:
                json.dump(body, handle, indent=2)
                handle.write("\n")

            os.replace(tmp, path)
        except BaseException:
            # A half-written temp file still holds a credential; do not leave it.
            try:
                os.unlink(tmp)
            except OSError:
                pass

            raise

        return path

    def get(self, name: str | None) -> Profile | None:
        return self.profiles.get(name or self.default_profile)


@dataclass
class Settings:
    """The resolved answer to "which site, which credential, how patient"."""

    base_url: str
    credential: Credential
    profile_name: str
    timeout: float = 60.0
    retries: int = 3
    retry_wait_max: float = 120.0
    verify_tls: bool = True
    debug: bool = False
    dry_run: bool = False


def _env(name: str) -> str | None:
    value = os.environ.get(ENV_PREFIX + name)

    return value if value else None


def resolve(args: Any) -> Settings:
    """Fold flags, environment and the stored profile into one Settings.

    Flags win over the environment, which wins over the profile. A missing
    credential is an error here rather than a 401 later — the difference matters
    because a 401 reads as "your key is wrong" when in fact none was found.
    """

    config = Config.load()

    profile_name = (
        getattr(args, "profile", None) or _env("PROFILE") or config.default_profile
    )
    profile = config.profiles.get(profile_name)

    base_url = (
        getattr(args, "base_url", None)
        or _env("BASE_URL")
        or (profile.base_url if profile else None)
        or DEFAULT_BASE_URL
    )

    credential = _resolve_credential(args, profile, profile_name)

    options = dict(profile.options) if profile else {}

    def opt(name: str, default: Any) -> Any:
        flag = getattr(args, name, None)

        if flag is not None:
            return flag

        env = _env(name.upper())

        if env is not None:
            return type(default)(env)

        return options.get(name, default)

    return Settings(
        base_url=base_url.rstrip("/"),
        credential=credential,
        profile_name=profile_name,
        timeout=float(opt("timeout", 60.0)),
        retries=int(opt("retries", 3)),
        retry_wait_max=float(opt("retry_wait_max", 120.0)),
        verify_tls=not getattr(args, "insecure", False),
        debug=bool(getattr(args, "debug", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
    )


def resolve_unkeyed(args: Any) -> Settings:
    """Everything `resolve` works out, minus the credential.

    For the endpoints that take none. `resolve` treats a missing credential as an
    error on purpose — a 401 later reads as "your key is wrong" when in fact
    none was found — but that is the wrong answer for `tmc contract sync`, which
    a freshly installed CLI runs BEFORE `tmc auth login` and which sends no
    `Authorization` header in any case.

    Same precedence for everything else, so `--base-url`, `--insecure`,
    `--timeout` and the profile all behave exactly as they do elsewhere.
    """

    config = Config.load()

    profile_name = (
        getattr(args, "profile", None) or _env("PROFILE") or config.default_profile
    )
    profile = config.profiles.get(profile_name)

    base_url = (
        getattr(args, "base_url", None)
        or _env("BASE_URL")
        or (profile.base_url if profile else None)
        or DEFAULT_BASE_URL
    )

    options = dict(profile.options) if profile else {}

    def opt(name: str, default: Any) -> Any:
        flag = getattr(args, name, None)

        if flag is not None:
            return flag

        env = _env(name.upper())

        if env is not None:
            return type(default)(env)

        return options.get(name, default)

    return Settings(
        base_url=base_url.rstrip("/"),
        credential=AnonymousCredential(),
        profile_name=profile_name,
        timeout=float(opt("timeout", 60.0)),
        retries=int(opt("retries", 3)),
        retry_wait_max=float(opt("retry_wait_max", 120.0)),
        verify_tls=not getattr(args, "insecure", False),
        debug=bool(getattr(args, "debug", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
    )


def _resolve_credential(
    args: Any, profile: Profile | None, profile_name: str
) -> Credential:
    # `--anon` is a positive choice, so it beats everything — including a
    # perfectly good stored key. Sending the key would be a DIFFERENT request:
    # the keyed surface returns the whole record, the anonymous one a summary,
    # and a flag that silently upgraded you would make "what does the public
    # see?" unanswerable.
    if getattr(args, "anon", False):
        return AnonymousCredential()

    token = getattr(args, "token", None) or _env("TOKEN")
    key_id = getattr(args, "key_id", None) or _env("KEY_ID")

    private_key_path = getattr(args, "private_key", None) or _env("PRIVATE_KEY_FILE")
    private_key_pem = _env("PRIVATE_KEY")

    lifetime = int(
        getattr(args, "jwt_lifetime", None)
        or _env("JWT_LIFETIME")
        or (profile.jwt_lifetime_sec if profile else 60)
    )

    # An explicitly supplied key id means JWT mode, whatever the profile says.
    if key_id or private_key_path or private_key_pem:
        if not key_id:
            raise ConfigError(
                "A private key was given without a key id.",
                hint="Pass --key-id tmcak_… (it is shown on the key in Account → API Keys).",
            )

        try:
            if private_key_path:
                signing_key = SigningKey.from_file(private_key_path)
            elif private_key_pem:
                signing_key = SigningKey.from_pem(private_key_pem)
            elif profile and profile.private_key_path:
                signing_key = SigningKey.from_file(profile.private_key_path)
            else:
                raise ConfigError(
                    "A key id was given without a private key.",
                    hint="Pass --private-key /path/to/key.pem or set TMC_PRIVATE_KEY.",
                )
        except KeyError_ as err:
            raise ConfigError(str(err)) from err

        return JwtCredential(key_id, signing_key, lifetime)

    if token:
        return BearerCredential(token)

    if profile:
        return profile.credential()

    raise ConfigError(
        f"No credentials found (profile '{profile_name}' does not exist).",
        hint=(
            "Run 'tmc auth login --token tmc_…' for a bearer key, or "
            "'tmc auth login --jwt --key-id tmcak_… --private-key key.pem' for a JWT key. "
            "TMC_TOKEN also works."
        ),
    )


def site_url(args: Any, base_url: str) -> str:
    """The WEBSITE's origin, as opposed to the API's.

    Two things live only on the site: the pages `tmc open` sends a browser to,
    and tRPC — which the API container refuses outright (`SiteSurfaceRefusal`
    in website-city), so `tmc defcon` cannot ask the API origin for the status
    page's data even though the route has the same name on both.

    Flag, then `TMC_SITE_URL`, then the profile's `site_url` option, then a
    guess from the base URL: `https://api.example.com` → `https://example.com`.
    Anything that is not an `api.` host is taken to BE the site, which is what a
    dev checkout or a bare container is — one origin answering both.
    """

    explicit = getattr(args, "site_url", None) or _env("SITE_URL")

    if not explicit:
        config = Config.load()
        name = getattr(args, "profile", None) or _env("PROFILE") or config.default_profile
        profile = config.profiles.get(name)

        if profile:
            explicit = profile.options.get("site_url")

    if explicit:
        return str(explicit).rstrip("/")

    import urllib.parse

    parts = urllib.parse.urlsplit(base_url.rstrip("/"))
    host = parts.hostname or ""

    if host.startswith("api.") and host.count(".") >= 2:
        # Rebuilt from the parsed host and port alone: a string replace on the
        # netloc could hit userinfo instead of the host, and userinfo has no
        # business travelling to the site origin anyway.
        netloc = host[len("api."):] + (f":{parts.port}" if parts.port else "")
        return urllib.parse.urlunsplit((parts.scheme, netloc, "", "", ""))

    return base_url.rstrip("/")
