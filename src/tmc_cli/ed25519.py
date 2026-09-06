"""Ed25519 signing for the content API's JWT auth mode.

The server (`@lib/jwt/ed25519`) accepts exactly one algorithm and hands the key
owner a PKCS#8 PEM private key at creation time. All this module has to do is
turn that PEM into 32 bytes of seed and sign 64-odd bytes of signing input.

TWO BACKENDS
------------
`cryptography` is used when it is installed, because a C implementation is
faster and is the one auditors already trust. It is NOT a requirement: a CLI
that cannot be run without a build toolchain is a CLI that does not get run, and
the fallback here is the RFC 8032 reference implementation, which is short
enough to read in one sitting. Both produce identical signatures — `doctor`
signs and verifies with whichever is active, so a broken fallback is loud.

Nothing here talks to the network and nothing writes a key to disk; the private
key stays wherever the caller put it.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re

# ---- PEM / DER --------------------------------------------------------------

# The one OID we accept, matching the server's refusal to negotiate: id-Ed25519,
# 1.3.101.112. A PEM carrying anything else is a key for a different algorithm
# and signing with it would produce a token no verifier could check.
_OID_ED25519 = bytes([0x06, 0x03, 0x2B, 0x65, 0x70])

_PEM_RE = re.compile(
    r"-----BEGIN ([A-Z0-9 ]+)-----(.+?)-----END \1-----", re.DOTALL
)


class KeyError_(ValueError):
    """A private key we could not read. Message is shown to the user verbatim."""


def _der_read_tlv(buf: bytes, at: int) -> tuple[int, bytes, int]:
    """Read one DER tag-length-value at `at`. Returns (tag, value, next offset)."""

    if at + 2 > len(buf):
        raise KeyError_("Truncated DER while reading the private key.")

    tag = buf[at]
    length = buf[at + 1]
    at += 2

    # Long form: the low 7 bits say how many bytes carry the real length.
    if length & 0x80:
        count = length & 0x7F

        if count == 0 or count > 4 or at + count > len(buf):
            raise KeyError_("Malformed DER length in the private key.")

        length = int.from_bytes(buf[at : at + count], "big")
        at += count

    if at + length > len(buf):
        raise KeyError_("Truncated DER value in the private key.")

    return tag, buf[at : at + length], at + length


def pem_to_seed(pem: str) -> bytes:
    """Extract the 32-byte Ed25519 seed from a PKCS#8 PEM private key.

    Walked as real DER rather than "take the last 32 bytes". That shortcut works
    for every key the site actually mints, and fails silently the first time one
    arrives with an attribute set or an encrypted body — silently, because the
    32 bytes it grabs are still 32 bytes and still sign, just not as this key.
    """

    match = _PEM_RE.search(pem)

    if not match:
        raise KeyError_(
            "Not a PEM private key (expected a '-----BEGIN PRIVATE KEY-----' block)."
        )

    label = match.group(1).strip()

    if "ENCRYPTED" in label:
        raise KeyError_(
            "Encrypted private keys are not supported. Decrypt it first "
            "(openssl pkcs8 -in key.pem -out plain.pem)."
        )

    try:
        der = base64.b64decode(re.sub(r"\s+", "", match.group(2)), validate=True)
    except Exception as err:  # noqa: BLE001 - surfaced as a user-facing message
        raise KeyError_(f"Private key body is not valid base64: {err}") from err

    # PrivateKeyInfo ::= SEQUENCE { version INTEGER, algorithm SEQUENCE, key OCTET STRING }
    tag, body, _ = _der_read_tlv(der, 0)

    if tag != 0x30:
        raise KeyError_("Private key is not a DER SEQUENCE (PKCS#8 expected).")

    _tag, _version, at = _der_read_tlv(body, 0)
    tag, algorithm, at = _der_read_tlv(body, at)

    if tag != 0x30 or _OID_ED25519 not in algorithm:
        raise KeyError_(
            "Private key is not Ed25519. The content API signs assertions with "
            "the Ed25519 key shown when the JWT-mode key was created."
        )

    tag, wrapper, _at = _der_read_tlv(body, at)

    if tag != 0x04:
        raise KeyError_("Private key payload is not an OCTET STRING.")

    # CurvePrivateKey is itself an OCTET STRING nested inside that one.
    tag, seed, _ = _der_read_tlv(wrapper, 0)

    if tag != 0x04 or len(seed) != 32:
        raise KeyError_(
            f"Expected a 32-byte Ed25519 seed, found {len(seed)} bytes."
        )

    return seed


# ---- Backend: cryptography --------------------------------------------------

try:  # pragma: no cover - depends on the host
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey as _CryptoKey,
    )

    HAVE_CRYPTOGRAPHY = True
except Exception:  # noqa: BLE001
    _CryptoKey = None  # type: ignore[assignment]
    HAVE_CRYPTOGRAPHY = False


# ---- Backend: RFC 8032 reference implementation ------------------------------

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)

# Base point, in extended homogeneous coordinates (x, y, z, t).
_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = 15112221349535400772501151409588531511454012693041857206046113283949847762202
_G = (_GX, _GY, 1, _GX * _GY % _P)

_Point = tuple[int, int, int, int]


def _sha512_modq(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little") % _L


def _point_add(p: _Point, q: _Point) -> _Point:
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P

    e, f, g, h = b - a, d - c, d + c, b + a

    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _point_mul(scalar: int, point: _Point) -> _Point:
    result: _Point = (0, 1, 1, 0)  # the neutral element

    while scalar > 0:
        if scalar & 1:
            result = _point_add(result, point)

        point = _point_add(point, point)
        scalar >>= 1

    return result


def _point_compress(point: _Point) -> bytes:
    z_inv = pow(point[2], _P - 2, _P)
    x = point[0] * z_inv % _P
    y = point[1] * z_inv % _P

    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _secret_expand(seed: bytes) -> tuple[int, bytes]:
    digest = hashlib.sha512(seed).digest()

    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8  # clamp: clear the low 3 bits and the top bit
    scalar |= 1 << 254

    return scalar, digest[32:]


def _sign_pure(seed: bytes, message: bytes) -> bytes:
    scalar, prefix = _secret_expand(seed)
    public = _point_compress(_point_mul(scalar, _G))

    r = _sha512_modq(prefix + message)
    big_r = _point_compress(_point_mul(r, _G))

    k = _sha512_modq(big_r + public + message)
    s = (r + k * scalar) % _L

    return big_r + int.to_bytes(s, 32, "little")


def _point_decompress(data: bytes) -> _Point | None:
    """Recover a point from its 32-byte encoding. Only used by the self-test."""

    if len(data) != 32:
        return None

    value = int.from_bytes(data, "little")
    sign = value >> 255
    y = value & ((1 << 255) - 1)

    if y >= _P:
        return None

    # x² = (y² - 1) / (d·y² + 1), then the square root of that.
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P

    if x2 == 0:
        # The only point with x = 0 is (0, 1); a set sign bit would be claiming
        # a negative zero, which does not exist.
        return None if sign else (0, y, 1, 0)

    x = pow(x2, (_P + 3) // 8, _P)

    # p ≡ 5 (mod 8), so that exponent gives either the root or the root times
    # sqrt(-1) — try the correction before giving up.
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P

    if (x * x - x2) % _P != 0:
        return None

    if x & 1 != sign:
        x = _P - x

    return (x, y, 1, x * y % _P)


def _verify_pure(public: bytes, message: bytes, signature: bytes) -> bool:
    """Verify a signature. Present so `doctor` can prove the backend works."""

    if len(signature) != 64:
        return False

    point_a = _point_decompress(public)
    point_r = _point_decompress(signature[:32])

    if point_a is None or point_r is None:
        return False

    s = int.from_bytes(signature[32:], "little")

    if s >= _L:
        return False

    k = _sha512_modq(signature[:32] + public + message)

    left = _point_mul(s, _G)
    right = _point_add(point_r, _point_mul(k, point_a))

    # Compare in affine terms: x1/z1 == x2/z2 and y1/z1 == y2/z2.
    return (
        (left[0] * right[2] - right[0] * left[2]) % _P == 0
        and (left[1] * right[2] - right[1] * left[2]) % _P == 0
    )


# ---- Public surface ---------------------------------------------------------


class SigningKey:
    """An Ed25519 private key, loaded once and reused for every request."""

    def __init__(self, seed: bytes) -> None:
        if len(seed) != 32:
            raise KeyError_("An Ed25519 seed must be exactly 32 bytes.")

        self._seed = seed
        self._crypto = _CryptoKey.from_private_bytes(seed) if HAVE_CRYPTOGRAPHY else None

    @classmethod
    def from_pem(cls, pem: str) -> "SigningKey":
        return cls(pem_to_seed(pem))

    @classmethod
    def from_file(cls, path: str) -> "SigningKey":
        expanded = os.path.expanduser(path)

        try:
            with open(expanded, "r", encoding="utf-8") as handle:
                return cls.from_pem(handle.read())
        except OSError as err:
            raise KeyError_(f"Cannot read private key '{expanded}': {err}") from err

    @property
    def backend(self) -> str:
        return "cryptography" if self._crypto is not None else "pure-python"

    def sign(self, message: bytes) -> bytes:
        if self._crypto is not None:
            return self._crypto.sign(message)

        return _sign_pure(self._seed, message)

    def public_bytes(self) -> bytes:
        if self._crypto is not None:
            return self._crypto.public_key().public_bytes_raw()

        scalar, _prefix = _secret_expand(self._seed)

        return _point_compress(_point_mul(scalar, _G))

    def self_test(self) -> bool:
        """Sign a probe and verify it, so a broken backend fails here not on-wire."""

        probe = b"tmc-cli ed25519 self test"

        return _verify_pure(self.public_bytes(), probe, self.sign(probe))


def generate_seed() -> bytes:
    """A fresh seed. Only used by `doctor`, which needs a key it can throw away."""

    return os.urandom(32)
