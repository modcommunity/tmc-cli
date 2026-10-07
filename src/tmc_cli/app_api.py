"""The app API (`/api/app/v1`) as the signed-in member: an app-user session.

`tmc catalog` reads the app API's public half with no credential. The other
half — friends, parties, presence, installs, subscriptions, writing reviews,
`stats/*` — needs an app-user token, which only the DEVICE FLOW hands out
(website-city `src/app/api/app/v1/auth/{device,token}`): the CLI opens a grant,
the member approves it in a browser, and the poll returns an access token
(`tmc_at_…`, one hour) plus a refresh token (`tmc_rt_…`, sixty days). A content
key cannot stand in for it — the app API resolves every `Bearer` as one of its
own tokens.

The pair lives in the profile (`Profile.app`, the same 0600 config file as the
content credential). `AppSession.request` is the one place that presents it:

  - an access token past its `expires_at` is rotated BEFORE the request, and a
    `401` is rotated after it — either way at most once per request, then the
    request fails with a sign-in hint rather than looping;
  - rotation (`auth/refresh`) returns a NEW refresh token and retires the old
    one (`RotateAppToken`; replaying a retired one revokes the whole family), so
    the new pair is saved before anything else happens;
  - the token is bound to the base URL it was issued by and is never sent
    anywhere else, whatever `--base-url` says.

`TMC_APP_TOKEN` supplies an access token directly (CI, a token minted
elsewhere). It is used as-is: nothing to refresh with, nothing saved.

Errors on this API are a flat envelope, `{ok:false, code, message}`
(`apiError` in `src/lib/app-api/handler.ts`); `http.py` unwraps it.
"""

from __future__ import annotations

import os
import time
import urllib.parse
from typing import Any

from .auth import AnonymousCredential, AppTokenCredential
from .config import Config, Profile, resolve_unkeyed
from .errors import ApiError, CliError, ConfigError
from .http import Response, Transport

API = "/api/app/v1"

#: Rotate this many seconds before the stated expiry, so a token does not die
#: between the check and the request.
EXPIRY_SKEW_SEC = 30

LOGIN_HINT = "Run: tmc auth app-login"


def query_string(params: dict[str, Any]) -> str:
    """The app API's query dialect: a list is a REPEATED key, a bool is true/false.

    `encode_params` joins lists with commas, which the content API splits and
    the app API does not — `parseQuery` there takes `?tags=1,2` as the one
    string "1,2" and the schema then refuses it. (`presence?userIds=` is the
    exception that proves it: that schema is one comma-separated STRING, so its
    caller joins the list itself.)
    """

    pairs: list[tuple[str, str]] = []

    for key, value in params.items():
        if value is None or value == [] or value == "":
            continue

        if isinstance(value, bool):
            pairs.append((key, "true" if value else "false"))
        elif isinstance(value, (list, tuple)):
            pairs.extend((key, str(v)) for v in value)
        else:
            pairs.append((key, str(value)))

    return urllib.parse.urlencode(pairs)


def target(path: str, params: dict[str, Any] | None = None) -> str:
    query = query_string(params or {})

    return f"{API}{path}" + (f"?{query}" if query else "")


def _transport(settings: Any, credential: Any, *, retries: int | None = None) -> Transport:
    return Transport(
        settings.base_url,
        credential,
        timeout=settings.timeout,
        retries=settings.retries if retries is None else retries,
        retry_wait_max=settings.retry_wait_max,
        verify_tls=settings.verify_tls,
        debug=settings.debug,
        dry_run=settings.dry_run,
    )


def anonymous_transport(args: Any) -> Transport:
    """For the routes that take no bearer: device start, poll, refresh."""

    settings = resolve_unkeyed(args)

    return _transport(settings, AnonymousCredential())


def anonymous_post(transport: Transport, path: str, body: dict[str, Any]) -> Response:
    """POST through a credential-less transport.

    `Transport.request` refuses every non-GET on an anonymous transport, which
    is right for the content API (its unauthenticated surface is read-only) and
    wrong here: `auth/device`, `auth/token` and `auth/refresh` are POSTs that
    must carry NO session — the refresh token in the body is the credential.
    So the guard is stepped around for exactly these calls, by giving the
    transport a credential that is not anonymous but still sends no header.
    """

    original = transport.credential
    transport.credential = _NoHeader()

    try:
        return transport.request("POST", target(path), json_body=body)
    finally:
        transport.credential = original


class _NoHeader(AnonymousCredential):
    kind = "none"


def profile_name(args: Any, config: Config) -> str:
    return getattr(args, "profile", None) or os.environ.get("TMC_PROFILE") or config.default_profile


def store_session(args: Any, base_url: str, token: dict[str, Any]) -> tuple[str, str]:
    """Write a `TokenResponse` into the profile. Returns (profile name, path).

    Re-reads the config first: the file may have changed since this command
    started (another `tmc` rotated the pair, `auth login` added a key), and
    saving a stale copy would put back a refresh token the server has retired.
    """

    config = Config.load()
    name = profile_name(args, config)
    profile = config.profiles.get(name)

    if profile is None:
        # A profile that holds only an app session is a real thing: somebody
        # who never publishes and only wants friends and parties.
        profile = Profile(name=name, base_url=base_url)
        config.profiles[name] = profile

        if len(config.profiles) == 1:
            config.default_profile = name

    user = token.get("user") or {}

    profile.app = {
        "base_url": base_url,
        "access_token": token["accessToken"],
        "refresh_token": token["refreshToken"],
        "expires_at": int(time.time()) + int(token.get("expiresIn") or 3600),
        "user": {k: user.get(k) for k in ("id", "username", "name")},
    }

    return name, config.save()


def forget_session(args: Any) -> bool:
    config = Config.load()
    profile = config.profiles.get(profile_name(args, config))

    if profile is None or not profile.app:
        return False

    profile.app = None
    config.save()

    return True


class AppSession:
    """Presents the stored app-user token, rotating it once when it has died."""

    def __init__(self, args: Any) -> None:
        self.args = args
        self.settings = resolve_unkeyed(args)

        env_token = os.environ.get("TMC_APP_TOKEN")

        if env_token:
            self.session: dict[str, Any] | None = None
            self.access = env_token
            return

        config = Config.load()
        name = profile_name(args, config)
        profile = config.profiles.get(name)

        if profile is None or not profile.app:
            raise ConfigError(
                f"Profile '{name}' has no app-user session.",
                hint=LOGIN_HINT + " (or set TMC_APP_TOKEN)",
            )

        bound = str(profile.app.get("base_url") or "").rstrip("/")

        if bound != self.settings.base_url:
            # Never presented to another host: a token is good until revoked,
            # and --base-url is exactly where a typo sends it somewhere else.
            raise ConfigError(
                f"Profile '{name}' signed in at {bound}, not {self.settings.base_url}.",
                hint=f"Use that base URL, or run 'tmc auth app-login --base-url {self.settings.base_url}'.",
            )

        self.session = profile.app
        self.access = str(profile.app.get("access_token") or "")

    @property
    def user(self) -> dict[str, Any]:
        return dict((self.session or {}).get("user") or {})

    def _expired(self) -> bool:
        if self.session is None:
            return False

        expires = self.session.get("expires_at")

        return isinstance(expires, (int, float)) and time.time() >= expires - EXPIRY_SKEW_SEC

    def refresh(self) -> None:
        """Rotate the pair (`auth/refresh`) and save it before anything else."""

        if not self.session or not self.session.get("refresh_token"):
            raise CliError(
                "The app-user token has expired and there is no refresh token to renew it.",
                hint=LOGIN_HINT,
            )

        try:
            resp = anonymous_post(
                _transport(self.settings, AnonymousCredential()),
                "/auth/refresh",
                {"refreshToken": self.session["refresh_token"]},
            )
        except ApiError as err:
            if err.status in (400, 401):
                # `invalid_grant` (expired, revoked) or `token_reuse` (the whole
                # family was signed out). The stored pair is dead either way;
                # keeping it would only make every later command fail the same
                # way, one refresh attempt later.
                forget_session(self.args)
                raise CliError(
                    f"The app session could not be renewed: {err.message}",
                    hint=LOGIN_HINT,
                ) from None

            raise

        if self.settings.dry_run:
            return

        token = resp.data or {}
        name, _path = store_session(self.args, self.settings.base_url, token)
        profile = Config.load().profiles.get(name)

        self.access = token["accessToken"]
        self.session = profile.app if profile else None

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        """One call, refreshed at most once. Returns the envelope's `data`."""

        refreshed = False

        if self._expired() and not self.settings.dry_run:
            self.refresh()
            refreshed = True

        while True:
            transport = _transport(self.settings, AppTokenCredential(self.access))

            try:
                return transport.request(method, target(path, params), json_body=json_body).data
            except ApiError as err:
                # `token_expired` is the one 401 a refresh can fix; `unauthorized`
                # (no session at all) and a 403 cannot, so neither is retried.
                if err.status != 401 or refreshed or self.session is None:
                    if err.status == 401:
                        err.hint = LOGIN_HINT

                    raise

                self.refresh()
                refreshed = True
