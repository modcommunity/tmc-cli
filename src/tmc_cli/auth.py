"""Credentials for the content API — both shapes the server accepts.

The server decides which shape you sent by its SYNTAX, not by anything you
declare (`@lib/api/public/auth.ts`): a bearer secret is `tmc_` plus hex, an
assertion is three base64url segments. So there is nothing to negotiate here
either — a credential object just knows how to produce one `Authorization`
header value, and the transport asks for a fresh one on every attempt.

That "every attempt" is not incidental. A JWT-mode key's `jti` is consumed on
first use and remembered for the length of the window, so replaying the exact
header of a retried request is a `400`, not a duplicate. Signing per attempt is
what makes retries safe, and it is why this is an interface rather than a string
computed once at startup.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass

from .ed25519 import SigningKey

# The server refuses `exp - iat` above 300s whatever the token declares, and
# checks both against its own clock with 300s of skew tolerance. Sixty seconds
# is comfortably inside that and short enough that a captured request is stale
# by the time anybody notices it.
DEFAULT_JWT_LIFETIME_SEC = 60


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_json(value: object) -> str:
    # Separators without spaces: the signing input is the encoded bytes, so any
    # incidental whitespace is signed too. Compact is simply the convention.
    return _b64u(json.dumps(value, separators=(",", ":")).encode("utf-8"))


class Credential:
    """Something that can authorize a request."""

    #: Shown by `auth list` / `whoami`.
    kind = "none"

    def authorization(self) -> str | None:
        """The `Authorization` value, or None to send no header at all."""

        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


class AnonymousCredential(Credential):
    """No credential — for the API's unauthenticated read surface.

    Deliberately a credential rather than a `None` threaded through the
    transport: `IsAnonRequest` keys on the ABSENCE of the header, so "send
    nothing" is a positive instruction the transport has to follow exactly. An
    empty or malformed header is not anonymous — it is the keyed surface being
    told the key is rubbish, which answers 401 rather than the public summary.
    """

    kind = "anonymous"

    def authorization(self) -> str | None:
        return None

    def describe(self) -> str:
        return "anonymous (no key)"


@dataclass
class BearerCredential(Credential):
    """A `tmc_` secret, presented verbatim on every request."""

    token: str

    kind = "bearer"

    def authorization(self) -> str | None:
        return f"Bearer {self.token}"

    def describe(self) -> str:
        return f"bearer {redact(self.token)}"


class JwtCredential(Credential):
    """A JWT-mode key: an Ed25519 private key that signs a fresh assertion."""

    kind = "jwt"

    def __init__(
        self,
        key_id: str,
        signing_key: SigningKey,
        lifetime_sec: int = DEFAULT_JWT_LIFETIME_SEC,
    ) -> None:
        self.key_id = key_id
        self.signing_key = signing_key
        self.lifetime_sec = max(5, min(300, lifetime_sec))

    def assertion(self, now: int | None = None) -> str:
        """Mint one assertion. Every call produces a new `jti`."""

        issued = int(time.time()) if now is None else now

        header = _b64u_json({"alg": "EdDSA", "typ": "JWT", "kid": self.key_id})
        payload = _b64u_json(
            {
                "iss": self.key_id,
                "iat": issued,
                "exp": issued + self.lifetime_sec,
                # 16 random bytes. The server remembers it for the length of the
                # window, so reuse — including an accidental one from a retry
                # that resent the same header — is refused.
                "jti": os.urandom(16).hex(),
            }
        )

        signing_input = f"{header}.{payload}"
        signature = self.signing_key.sign(signing_input.encode("ascii"))

        return f"{signing_input}.{_b64u(signature)}"

    def authorization(self) -> str | None:
        return f"Bearer {self.assertion()}"

    def describe(self) -> str:
        return f"jwt {self.key_id} ({self.signing_key.backend})"


def redact(secret: str) -> str:
    """Enough of a secret to recognise it, never enough to use it."""

    if len(secret) <= 12:
        return "*" * len(secret)

    return f"{secret[:8]}…{secret[-4:]}"


def looks_like_bearer(value: str) -> bool:
    """The server's own rule: `tmc_` plus hex is a bearer secret."""

    return value.startswith("tmc_")


def looks_like_assertion(value: str) -> bool:
    return value.count(".") == 2 and all(
        part and all(c.isalnum() or c in "-_" for c in part)
        for part in value.split(".")
    )
