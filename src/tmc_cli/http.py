"""The transport: one place that knows how to talk to `/api/content`.

Deliberately stdlib-only. This CLI's whole job is to be the thing you can drop
onto a build machine or a game server and run, and `pip install requests` is a
step that fails on exactly the boxes where you need it most.

WHAT IT AUTOMATES
-----------------
- **A fresh credential per attempt.** A JWT assertion carries a one-shot `jti`;
  resending a retried request with the same header is a `400`. The header is
  therefore built inside the retry loop, never outside it.
- **Rate limits.** A `429` names its own wait ("Try again in 42s"), so a retry
  waits exactly that long rather than guessing — but only up to
  `--retry-wait-max`, because the 7-day window's answer can be hours and a CLI
  that silently sleeps through lunch is worse than one that says so.
- **Transient failures.** Connection resets and 5xx get exponential backoff.
  Nothing else is retried: a 4xx means the request was wrong, and sending it
  again is just a slower way to be wrong.
- **Streaming uploads.** A multipart body reads from disk in chunks, so a 1 GB
  release file does not have to fit in memory first.
"""

from __future__ import annotations

import json
import mimetypes
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Iterable, Sequence

from .auth import Credential
from .errors import ApiError, NetworkError, UsageError
from .version import USER_AGENT

# Methods that are safe to send again after a transient failure. A POST is not
# on the list: the API's creates are not idempotent, and a reply we never saw is
# not proof the write did not land.
RETRY_SAFE_METHODS = {"GET", "PUT", "DELETE", "HEAD"}

# "Rate limit exceeded. Try again in 42s." — how the KEYED surface states its
# wait, since it sets no `retry-after`. (The anonymous surface does set one, and
# `_retry_after` is preferred over this when it is there.)
_RATE_WAIT_RE = re.compile(r"try again in\s+(\d+)\s*s", re.IGNORECASE)

_CHUNK = 1024 * 256


@dataclass
class Response:
    status: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def data(self) -> Any:
        """The `data` envelope every successful response wraps its payload in."""

        if isinstance(self.body, dict) and "data" in self.body:
            return self.body["data"]

        return self.body

    @property
    def pagination(self) -> dict[str, Any] | None:
        if isinstance(self.body, dict):
            page = self.body.get("pagination")

            if isinstance(page, dict):
                return page

        return None


# ---- Multipart --------------------------------------------------------------


class _MultipartBody:
    """A streaming `multipart/form-data` body.

    Implements just enough of a file object for `http.client` to pump it: a
    `read(n)` that walks a list of parts, where a part is either literal bytes
    (a header block, a text field) or a path to stream from disk.
    """

    def __init__(self, parts: Sequence[bytes | str]) -> None:
        self._parts = list(parts)
        self._index = 0
        self._handle: BinaryIO | None = None

    def __len__(self) -> int:
        total = 0

        for part in self._parts:
            total += len(part) if isinstance(part, bytes) else os.path.getsize(part)

        return total

    def read(self, size: int = -1) -> bytes:
        out = bytearray()

        while size < 0 or len(out) < size:
            if self._index >= len(self._parts):
                break

            part = self._parts[self._index]
            want = _CHUNK if size < 0 else size - len(out)

            if isinstance(part, bytes):
                out += part
                self._index += 1
                continue

            if self._handle is None:
                self._handle = open(part, "rb")

            chunk = self._handle.read(want)

            if not chunk:
                self._handle.close()
                self._handle = None
                self._index += 1
                continue

            out += chunk

        return bytes(out)

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


@dataclass
class FilePart:
    """One file part of a multipart upload."""

    path: str
    field_name: str = "file"
    filename: str | None = None
    content_type: str | None = None

    def resolved_name(self) -> str:
        return self.filename or os.path.basename(self.path)

    def resolved_type(self) -> str:
        if self.content_type:
            return self.content_type

        guessed, _encoding = mimetypes.guess_type(self.resolved_name())

        return guessed or "application/octet-stream"


def build_multipart(
    files: Sequence[FilePart], fields: dict[str, str] | None = None
) -> tuple[str, _MultipartBody]:
    """Encode parts into a streaming body. Returns (content type, body)."""

    boundary = f"----tmc-cli-{uuid.uuid4().hex}"
    parts: list[bytes | str] = []

    for name, value in (fields or {}).items():
        if value is None:
            continue

        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )

    for part in files:
        # The server accepts any part that carries a file whatever the field is
        # called, so the name here is cosmetic — it shows up in nothing but a
        # packet capture.
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{part.field_name}"; '
                f'filename="{_escape(part.resolved_name())}"\r\n'
                f"Content-Type: {part.resolved_type()}\r\n\r\n"
            ).encode("utf-8")
        )
        parts.append(part.path)
        parts.append(b"\r\n")

    parts.append(f"--{boundary}--\r\n".encode("utf-8"))

    return f"multipart/form-data; boundary={boundary}", _MultipartBody(parts)


def _escape(name: str) -> str:
    return name.replace('"', "%22").replace("\r", "").replace("\n", "")


# ---- Client -----------------------------------------------------------------


class Transport:
    def __init__(
        self,
        base_url: str,
        credential: Credential,
        *,
        timeout: float = 60.0,
        retries: int = 3,
        retry_wait_max: float = 120.0,
        verify_tls: bool = True,
        debug: bool = False,
        dry_run: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.credential = credential
        self.timeout = timeout
        self.retries = max(0, retries)
        self.retry_wait_max = retry_wait_max
        self.debug = debug
        self.dry_run = dry_run

        context = ssl.create_default_context()

        if not verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context),
            # Redirects are not followed: a 3xx from an API that never issues one
            # means the base URL is wrong (an http:// origin, a trailing /api),
            # and quietly following it can replay a credential to another host.
            _NoRedirect(),
        )

    @property
    def anonymous(self) -> bool:
        """Whether this transport sends no credential at all."""

        return self.credential.kind == "anonymous"

    # -- request building ----------------------------------------------------

    def url_for(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = f"{self.base_url}/{path.lstrip('/')}"
        query = encode_params(params or {})

        return f"{url}?{query}" if query else url

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        files: Sequence[FilePart] | None = None,
        fields: dict[str, str] | None = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Response:
        method = method.upper()
        url = self.url_for(path, params)

        # Refused here rather than at each call site: the unauthenticated surface
        # is GET-only for the seven content items, and every other verb is a 401
        # from the keyed handler that reads as "your key is wrong" when the real
        # answer is "you did not send one".
        if self.anonymous and method != "GET":
            raise UsageError(
                f"{method} needs a key — the anonymous surface is read-only.",
                hint="Drop --anon, or run 'tmc auth login --token tmc_…'.",
            )

        if self.dry_run:
            return self._describe_dry_run(method, url, json_body, files, raw_body)

        attempt = 0

        while True:
            attempt += 1

            body: Any = None
            headers = {
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            }

            # Built inside the loop on purpose — see the module docstring. None
            # means send NO header: `IsAnonRequest` keys on its absence, and an
            # empty one would be read as a bad key and answered 401 instead of
            # taking the unauthenticated branch.
            authorization = self.credential.authorization()

            if authorization is not None:
                headers["Authorization"] = authorization

            if json_body is not None:
                body = json.dumps(json_body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            elif files is not None:
                ctype, multipart = build_multipart(files, fields)
                body = multipart
                headers["Content-Type"] = ctype
                headers["Content-Length"] = str(len(multipart))
            elif raw_body is not None:
                body = raw_body
                headers["Content-Type"] = content_type or "application/octet-stream"

            headers.update(extra_headers or {})

            if self.debug:
                self._log(method, url, headers, json_body)

            try:
                return self._send(method, url, headers, body)
            except ApiError as err:
                wait = self._retry_delay(err, method, attempt)

                if wait is None:
                    raise

                self._warn(
                    f"HTTP {err.status} — retrying in {wait:.0f}s "
                    f"(attempt {attempt}/{self.retries + 1})"
                )
                time.sleep(wait)
            except NetworkError:
                if method not in RETRY_SAFE_METHODS or attempt > self.retries:
                    raise

                wait = _backoff(attempt)
                self._warn(f"Connection failed — retrying in {wait:.0f}s")
                time.sleep(wait)
            finally:
                if isinstance(body, _MultipartBody):
                    body.close()

    # -- transport -----------------------------------------------------------

    def _send(
        self, method: str, url: str, headers: dict[str, str], body: Any
    ) -> Response:
        request = urllib.request.Request(url, data=body, method=method)

        for name, value in headers.items():
            request.add_header(name, value)

        try:
            with self._opener.open(request, timeout=self.timeout) as raw:
                payload = raw.read()
                parsed = _parse_json(payload)

                return Response(
                    status=raw.status,
                    body=parsed,
                    headers={k.lower(): v for k, v in raw.headers.items()},
                )
        except urllib.error.HTTPError as err:
            payload = err.read()
            parsed = _parse_json(payload)

            message = "Request failed."
            issues = None
            index = None
            data = None
            code = None

            if isinstance(parsed, dict):
                message = str(parsed.get("error") or message)
                issues = parsed.get("issues")
                index = parsed.get("index")
                data = parsed.get("data")
                # The unauthenticated surface pairs every refusal with a stable
                # machine-readable `code`; the keyed one has never sent any.
                code = parsed.get("code")
            elif isinstance(parsed, str) and parsed.strip():
                # An HTML error page means we did not reach the API at all —
                # usually a wrong base URL or a proxy in front of it.
                message = _summarize_non_json(parsed, err.code)

            raise ApiError(
                err.code,
                message,
                issues=issues,
                index=index,
                data=data,
                code=code,
                retry_after=_retry_after(err.headers),
                method=method,
                url=url,
            ) from None
        except urllib.error.URLError as err:
            raise NetworkError(
                f"Could not reach {url}: {err.reason}",
                hint="Check --base-url, your network, and (for a local dev site) --insecure.",
            ) from None
        except (TimeoutError, OSError) as err:
            raise NetworkError(f"Could not reach {url}: {err}") from None

    # -- retry policy --------------------------------------------------------

    def _retry_delay(self, err: ApiError, method: str, attempt: int) -> float | None:
        """Seconds to wait before retrying, or None to give up and raise."""

        if attempt > self.retries:
            return None

        if err.status == 429:
            # `retry-after` first: the anonymous surface sets it, and a header is
            # a better answer than a regex over an English sentence. The keyed
            # surface sets no header, so the sentence is still the fallback.
            wait = err.retry_after

            if wait is None:
                wait = _rate_limit_wait(err.message)

            if wait is None:
                wait = _backoff(attempt)

            if wait > self.retry_wait_max:
                # Say why rather than sleeping through it. Both API windows are
                # long (5h / 7d), so "wait it out" is not always a real option
                # and pretending otherwise hangs a build.
                err.hint = (
                    f"The window resets in {wait:.0f}s, above --retry-wait-max "
                    f"({self.retry_wait_max:.0f}s). Raise it to wait it out."
                )
                return None

            return wait

        if err.status >= 500 and method in RETRY_SAFE_METHODS:
            return _backoff(attempt)

        return None

    # -- diagnostics ---------------------------------------------------------

    def _describe_dry_run(
        self,
        method: str,
        url: str,
        json_body: Any,
        files: Sequence[FilePart] | None,
        raw_body: bytes | None,
    ) -> Response:
        print(f"DRY RUN  {method} {url}", file=sys.stderr)

        if json_body is not None:
            print(json.dumps(json_body, indent=2), file=sys.stderr)

        for part in files or []:
            size = os.path.getsize(part.path) if os.path.exists(part.path) else 0
            print(
                f"  file {part.path} ({_human_size(size)}, {part.resolved_type()})",
                file=sys.stderr,
            )

        if raw_body is not None:
            print(f"  {len(raw_body)} bytes of raw body", file=sys.stderr)

        return Response(status=0, body={"data": None, "dryRun": True})

    def _log(
        self, method: str, url: str, headers: dict[str, str], json_body: Any
    ) -> None:
        safe = dict(headers)

        if "Authorization" in safe:
            safe["Authorization"] = _redact_authorization(safe["Authorization"])
        else:
            safe["Authorization"] = "(none — anonymous read)"

        print(f"[tmc] {method} {url}", file=sys.stderr)

        for name, value in safe.items():
            print(f"[tmc]   {name}: {value}", file=sys.stderr)

        if json_body is not None:
            print(f"[tmc]   body: {json.dumps(json_body)[:2000]}", file=sys.stderr)

    def _warn(self, message: str) -> None:
        print(f"[tmc] {message}", file=sys.stderr)


def _redact_authorization(value: str) -> str:
    """Enough to tell two credentials apart, never enough to present one.

    This was `value[:24]`, which is `Bearer ` (7 characters) plus seventeen of
    the credential — for a `tmc_` bearer secret, thirteen characters of the hex
    itself. `--debug` writes to stderr, and stderr is what a CI job archives, so
    that is a secret prefix in a log file.

    What a reader of the log actually needs is which scheme and which kind of
    credential: `tmc_` says bearer secret, `eyJ` says a signed assertion. Four
    characters carry both, and the length distinguishes a truncated paste from a
    whole one.
    """

    scheme, sep, rest = value.partition(" ")

    if not sep or not rest:
        # No scheme to keep separate — treat the whole header as the secret.
        return f"(redacted, {len(value)} chars)"

    return f"{scheme} {rest[:4]}… (redacted, {len(rest)} chars)"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102
        return None


# ---- helpers ----------------------------------------------------------------


def encode_params(params: dict[str, Any]) -> str:
    """Query-string encoding that matches how the API reads list params.

    A list becomes one comma-joined value: `listParam` in the handler splits on
    commas AND accepts repeats, so either encoding works, and the comma form is
    the one the docs show and the one that stays readable in `--debug` output.
    """

    pairs: list[tuple[str, str]] = []

    for key, value in params.items():
        if value is None:
            continue

        if isinstance(value, bool):
            pairs.append((key, "1" if value else "0"))
        elif isinstance(value, (list, tuple, set)):
            items = [str(v) for v in value if v is not None and str(v) != ""]

            if items:
                pairs.append((key, ",".join(items)))
        else:
            pairs.append((key, str(value)))

    return urllib.parse.urlencode(pairs)


def _parse_json(payload: bytes) -> Any:
    if not payload:
        return None

    text = payload.decode("utf-8", errors="replace")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _summarize_non_json(text: str, status: int) -> str:
    stripped = re.sub(r"<[^>]+>", " ", text)
    stripped = re.sub(r"\s+", " ", stripped).strip()

    return (
        f"Server returned {status} with a non-JSON body "
        f"({stripped[:160] or 'empty'}). Is --base-url pointing at the site root?"
    )


def _retry_after(headers: Any) -> float | None:
    """`retry-after`, in seconds. Only the delta form — the API never sends a date."""

    try:
        raw = headers.get("retry-after")
    except AttributeError:
        return None

    if raw is None:
        return None

    try:
        return max(0.0, float(str(raw).strip()))
    except ValueError:
        return None


def _rate_limit_wait(message: str) -> float | None:
    match = _RATE_WAIT_RE.search(message or "")

    return float(match.group(1)) if match else None


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter, so parallel callers don't resynchronise."""

    return min(30.0, (2 ** (attempt - 1)) + random.uniform(0, 0.5))


def _human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"

        size /= 1024

    return f"{size:.1f} GB"


def chunked(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    """Split a list into batches — the API caps every bulk endpoint."""

    for start in range(0, len(items), size):
        yield items[start : start + size]
