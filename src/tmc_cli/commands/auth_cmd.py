"""`tmc auth …` — storing a credential once, and proving it works.

The API has no "describe my key" endpoint: a key carries `canRead` / `canWrite`
/ `canDelete`, an optional content scope and an optional IP whitelist, and none
of that is readable back. So `whoami` establishes it the only way available —
by making one deliberately harmless request per permission and reading the
refusal. The write probe sends a body that cannot validate (`{}` on a type whose
create schema has required fields) and the delete probe sends an empty id list,
so both are answered by the permission check first and by a `400` after it.
Nothing is created and nothing is deleted either way.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import stat
import sys

from .. import output
from ..auth import BearerCredential, JwtCredential, looks_like_bearer, redact
from ..config import Config, DEFAULT_BASE_URL, Profile, config_path
from ..context import Context
from ..ed25519 import KeyError_, SigningKey, generate_seed
from ..errors import ApiError, CliError, ConfigError, UsageError
from ..http import Transport
from ..schema import ANON_TYPES

# The messages a content-scoped key is refused with, so `whoami` can report the
# binding rather than just "403".
_SCOPE_TYPE_RE = re.compile(r"scoped to (\w+) items only")
_SCOPE_ITEM_RE = re.compile(r"scoped to (\w+) #(\d+)")
_IP_RE = re.compile(r"IP (\S+) is not permitted")


def login(ctx: Context) -> int:
    args = ctx.args
    config = Config.load()

    name = getattr(args, "profile", None) or config.default_profile
    base_url = (getattr(args, "base_url", None) or DEFAULT_BASE_URL).rstrip("/")

    if args.jwt or getattr(args, "key_id", None) or getattr(args, "private_key", None):
        profile = _login_jwt(args, name, base_url)
    else:
        profile = _login_bearer(args, name, base_url)

    if not args.no_verify:
        _verify(ctx, profile)

    config.profiles[name] = profile

    if args.set_default or len(config.profiles) == 1:
        config.default_profile = name

    path = config.save()

    ctx.done(f"Saved profile '{name}' → {path}")
    ctx.note(f"  site: {profile.base_url}")
    ctx.note(f"  auth: {profile.describe()}")

    if config.default_profile == name:
        ctx.note(f"  '{name}' is now the default profile.")

    return 0


def _login_bearer(args, name: str, base_url: str) -> Profile:
    token = getattr(args, "token", None)

    if not token:
        if not sys.stdin.isatty():
            raise UsageError(
                "No token given.",
                hint="Pass --token tmc_…, or run interactively to be prompted.",
            )

        # getpass so the secret never lands in the scrollback or the shell's
        # history file — which is the whole reason `--token` is not the only way.
        token = getpass.getpass("API token (tmc_…): ").strip()

    if not token:
        raise UsageError("An empty token is not a token.")

    if not looks_like_bearer(token):
        output.warn(
            "That does not look like a bearer token (they start with 'tmc_'). "
            "For a JWT-mode key use --jwt --key-id … --private-key …"
        )

    return Profile(name=name, base_url=base_url, auth_mode="bearer", token=token)


def _login_jwt(args, name: str, base_url: str) -> Profile:
    key_id = getattr(args, "key_id", None)
    private_key = getattr(args, "private_key", None)

    if not key_id:
        raise UsageError(
            "JWT mode needs the key id.",
            hint="It is shown on the key in Account → API Keys and starts with 'tmcak_'.",
        )

    if not private_key:
        raise UsageError(
            "JWT mode needs the private key.",
            hint="Pass --private-key /path/to/key.pem (the PKCS#8 PEM shown once at creation).",
        )

    path = os.path.abspath(os.path.expanduser(private_key))

    try:
        signing_key = SigningKey.from_file(path)
    except KeyError_ as err:
        raise ConfigError(str(err)) from err

    if not signing_key.self_test():
        raise ConfigError(
            "The private key loaded but failed a sign/verify self-test — refusing to save it."
        )

    if args.copy_key:
        # Keeping our own copy means the profile keeps working after the
        # original is moved, which is what people do with a file they were told
        # is shown once.
        destination = os.path.join(os.path.dirname(config_path()), f"{name}.pem")
        os.makedirs(os.path.dirname(destination), mode=0o700, exist_ok=True)

        shutil.copyfile(path, destination)
        os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)

        path = destination

    _warn_if_readable(path)

    if not key_id.startswith("tmcak_"):
        output.warn(
            f"'{key_id}' does not start with 'tmcak_'. Content-API key ids do; "
            "an integration key ('tmck_') belongs to a different API."
        )

    return Profile(
        name=name,
        base_url=base_url,
        auth_mode="jwt",
        key_id=key_id,
        private_key_path=path,
        jwt_lifetime_sec=int(getattr(args, "jwt_lifetime", None) or 60),
    )


def _warn_if_readable(path: str) -> None:
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return

    if mode & (stat.S_IRGRP | stat.S_IROTH):
        output.warn(f"{path} is readable by others — consider chmod 600.")


def _verify(ctx: Context, profile: Profile) -> None:
    """One cheap read, so a bad paste fails now rather than on the first real call."""

    transport = Transport(
        profile.base_url,
        profile.credential(),
        # Built from the flags directly rather than from ctx.settings: settings
        # resolve the credential we are in the middle of creating.
        timeout=float(getattr(ctx.args, "timeout", None) or 60.0),
        retries=0,
        verify_tls=not getattr(ctx.args, "insecure", False),
        debug=getattr(ctx.args, "debug", False),
    )

    try:
        transport.request("GET", "/api/content/asset", params={"limit": 1})
        ctx.note("Credential verified against " + profile.base_url)
    except ApiError as err:
        if err.status in (401, 403):
            raise CliError(
                f"The site rejected the credential: {err.message}",
                hint="Check the token/key id, or pass --no-verify to save it anyway.",
            ) from None

        output.warn(f"Could not verify the credential (HTTP {err.status}: {err.message})")


def list_profiles(ctx: Context) -> int:
    config = Config.load()

    if not config.profiles:
        ctx.note(f"No profiles yet ({config_path()}).")
        ctx.note("Run: tmc auth login --token tmc_…")
        return 0

    rows = [
        {
            "profile": name,
            "default": name == config.default_profile,
            "site": profile.base_url,
            "mode": profile.auth_mode,
            "credential": profile.describe(),
        }
        for name, profile in sorted(config.profiles.items())
    ]

    ctx.emit(rows, columns=("profile", "default", "site", "mode", "credential"))

    return 0


def use_profile(ctx: Context) -> int:
    config = Config.load()
    name = ctx.args.name

    if name not in config.profiles:
        raise UsageError(
            f"No profile named '{name}'.",
            hint=f"Known: {', '.join(sorted(config.profiles)) or 'none'}",
        )

    config.default_profile = name
    config.save()

    ctx.done(f"Default profile is now '{name}'.")

    return 0


def remove_profile(ctx: Context) -> int:
    config = Config.load()
    name = ctx.args.name

    profile = config.profiles.pop(name, None)

    if profile is None:
        raise UsageError(f"No profile named '{name}'.")

    # A copied key is ours to clean up; one the user pointed at is not.
    if profile.private_key_path and os.path.dirname(
        profile.private_key_path
    ) == os.path.dirname(config_path()):
        if ctx.confirm(f"Also delete the stored private key {profile.private_key_path}?"):
            try:
                os.remove(profile.private_key_path)
            except OSError as err:
                output.warn(f"Could not remove the key file: {err}")

    if config.default_profile == name and config.profiles:
        config.default_profile = sorted(config.profiles)[0]

    config.save()

    ctx.done(f"Removed profile '{name}'.")

    return 0


def whoami(ctx: Context) -> int:
    """Report which credential is in play and what the server lets it do."""

    settings = ctx.settings
    credential = settings.credential

    facts: dict[str, object] = {
        "profile": settings.profile_name,
        "site": settings.base_url,
        "authMode": credential.kind,
    }

    # There is no key to report on, and the three probes are two writes and a
    # read the anonymous surface would refuse on principle rather than on
    # permission — so probing would answer "no" three times and mean nothing.
    if credential.kind == "anonymous":
        facts["canRead"] = "public summaries only"
        facts["canWrite"] = False
        facts["canDelete"] = False
        facts["scope"] = f"the anonymous read surface ({', '.join(ANON_TYPES)})"

        ctx.emit(facts)

        if not ctx.quiet:
            output.info(
                "Anonymous mode sends no credential. Whether it answers at all "
                "is the site's own switch, and each item's team can opt out."
            )

        return 0

    if isinstance(credential, JwtCredential):
        facts["keyId"] = credential.key_id
        facts["signingBackend"] = credential.signing_key.backend
        facts["assertionLifetimeSec"] = credential.lifetime_sec
    elif isinstance(credential, BearerCredential):
        facts["token"] = redact(credential.token)

    transport = ctx.client.http

    read = _probe(transport, "GET", "/api/content/asset", params={"limit": 1})
    facts["canRead"] = read.allowed

    if read.detail:
        facts["read"] = read.detail

    if ctx.args.read_only:
        facts["canWrite"] = "not probed (--read-only)"
        facts["canDelete"] = "not probed (--read-only)"
    else:
        # `{}` cannot pass the asset create schema (name is required) and `[]`
        # cannot pass the bulk-delete schema (min 1) — so both are answered by
        # the permission check, then by validation. Neither writes anything.
        write = _probe(transport, "POST", "/api/content/asset", json_body={})
        delete = _probe(transport, "DELETE", "/api/content/asset", json_body=[])

        facts["canWrite"] = write.allowed
        facts["canDelete"] = delete.allowed

        for probe in (write, delete):
            if probe.scope:
                facts["scope"] = probe.scope
                break

    if read.scope and "scope" not in facts:
        facts["scope"] = read.scope

    ctx.emit(facts)

    if not ctx.quiet and not ctx.args.read_only:
        output.info(
            "Permissions are established by three harmless probe requests; "
            "they appear in the API audit log and count against the rate limit."
        )

    return 0


class _Probe:
    def __init__(self, allowed: object, detail: str = "", scope: str = "") -> None:
        self.allowed = allowed
        self.detail = detail
        self.scope = scope


def _probe(transport: Transport, method: str, path: str, **kwargs) -> _Probe:
    try:
        transport.request(method, path, **kwargs)
        return _Probe(True)
    except ApiError as err:
        # A 400 means the request got past every permission check and died on
        # its (deliberately invalid) payload — which is a "yes".
        if err.status == 400:
            return _Probe(True)

        if err.status == 403:
            scope = ""

            item = _SCOPE_ITEM_RE.search(err.message)
            kind = _SCOPE_TYPE_RE.search(err.message)
            ip = _IP_RE.search(err.message)

            if item:
                scope = f"{item.group(1)} #{item.group(2)}"
            elif kind:
                scope = f"{kind.group(1)} items only"

            if ip:
                return _Probe(
                    "blocked",
                    f"this key's IP whitelist does not include {ip.group(1)}",
                )

            return _Probe(False, err.message, scope)

        if err.status == 401:
            raise CliError(f"The site rejected the credential: {err.message}") from None

        return _Probe("unknown", f"HTTP {err.status}: {err.message}")


def show_token(ctx: Context) -> int:
    """Print an Authorization header value — for curl, or to debug a signature.

    On a JWT key this mints a REAL assertion with a real `jti`, so it is good for
    exactly one request. That is a property of the mode, not a limitation here.
    """

    credential = ctx.settings.credential
    value = credential.authorization()

    if ctx.args.header:
        print(f"Authorization: {value}")
    else:
        print(value.removeprefix("Bearer "))

    if isinstance(credential, JwtCredential) and not ctx.quiet:
        output.info(
            f"Valid for {credential.lifetime_sec}s and for ONE request — the jti is "
            "consumed on first use."
        )

    return 0


def doctor(ctx: Context) -> int:
    """Check the things that break before a request is ever sent."""

    ok = True

    print(f"python           {sys.version.split()[0]}")

    seed = generate_seed()
    key = SigningKey(seed)

    backend_ok = key.self_test()
    ok = ok and backend_ok

    print(f"ed25519 backend  {key.backend} {'OK' if backend_ok else 'FAILED'}")

    if key.backend == "pure-python":
        print(
            "                 (install 'cryptography' for faster signing; "
            "not required)"
        )

    path = config_path()
    print(f"config           {path} {'exists' if os.path.exists(path) else 'missing'}")

    if os.path.exists(path):
        mode = stat.S_IMODE(os.stat(path).st_mode)

        if mode & 0o077:
            print(f"                 WARNING: mode {oct(mode)} — should be 0600")
            ok = False

    try:
        settings = ctx.settings
        print(f"profile          {settings.profile_name}")
        print(f"site             {settings.base_url}")
        print(f"credential       {settings.credential.describe()}")
    except CliError as err:
        print(f"credential       NOT CONFIGURED — {err.message}")
        return 1

    try:
        ctx.client.http.request("GET", "/api/content/asset", params={"limit": 1})
        print("connectivity     OK (GET /api/content/asset)")
    except CliError as err:
        message = err.format() if isinstance(err, ApiError) else err.message
        print(f"connectivity     FAILED — {message}")
        ok = False

    return 0 if ok else 1
