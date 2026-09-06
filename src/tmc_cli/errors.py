"""Errors the CLI raises at itself, and the shape the API's errors arrive in.

Everything here is caught at the top of `cli.main` and printed as one sentence
plus an exit code — a traceback is a bug report, not an error message, and the
API already writes its refusals in prose meant for the person who sent them.
"""

from __future__ import annotations

from typing import Any

# Exit codes, so a shell script can branch without parsing text.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3  # 401 / 403 — the key is wrong or lacks the permission
EXIT_NOT_FOUND = 4  # 404
EXIT_VALIDATION = 5  # 400 — the payload was refused
EXIT_RATE_LIMIT = 6  # 429
EXIT_SERVER = 7  # 5xx
EXIT_NETWORK = 8
EXIT_ERROR = 1


#: What each documented `code` on the unauthenticated surface actually means for
#: the person who sent the request. `not_found` is deliberately absent: it does
#: not distinguish "no such id" from "not publicly readable", and inventing a
#: hint that guessed between them would be worse than the server's own sentence.
ANON_CODE_HINTS: dict[str, str] = {
    "auth_required": (
        "This type — or this whole site's anonymous surface — needs a key. "
        "Drop --anon and run 'tmc auth login'."
    ),
    "unknown_type": "No such content type. Run 'tmc schema' for the list.",
    "api_disabled": (
        "The item is public on the website but its team turned key-free API "
        "access off (apiPublic). A key belonging to somebody who can already "
        "see it is unaffected — drop --anon."
    ),
    "rate_limited": (
        "The anonymous quota is per source address and deliberately small. "
        "A key is free and has a much larger budget."
    ),
}


class CliError(Exception):
    """Anything we can explain in one line. Never carries a traceback to the user."""

    exit_code = EXIT_ERROR

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class UsageError(CliError):
    exit_code = EXIT_USAGE


class ConfigError(CliError):
    exit_code = EXIT_USAGE


class NetworkError(CliError):
    exit_code = EXIT_NETWORK


class ApiError(CliError):
    """A non-2xx response, with everything the server said about it.

    `issues` is zod's `flatten()` output when validation failed and `index` names
    which element of a bulk request was at fault — both are the difference
    between "Validation failed." and knowing which field of which item to fix, so
    they are carried through to the formatter rather than dropped.

    `code` is the unauthenticated surface's machine-readable refusal (the keyed
    one has never sent one). It is worth keeping separate from the sentence
    because two of its values mean "this is not your bug" — see ANON_CODE_HINTS.
    """

    def __init__(
        self,
        status: int,
        message: str,
        *,
        issues: Any = None,
        index: int | None = None,
        data: Any = None,
        code: str | None = None,
        retry_after: float | None = None,
        method: str = "",
        url: str = "",
    ) -> None:
        super().__init__(message)

        self.status = status
        self.issues = issues
        self.index = index
        self.data = data
        self.code = code
        self.retry_after = retry_after
        self.method = method
        self.url = url

        # `CliError.__init__` has just set this to None. A coded refusal knows
        # more about what to do next than the sentence does, so it fills it in —
        # and a caller that sets its own hint afterwards still wins.
        self.hint = ANON_CODE_HINTS.get(code or "")

    @property
    def exit_code(self) -> int:  # type: ignore[override]
        return {
            400: EXIT_VALIDATION,
            401: EXIT_AUTH,
            403: EXIT_AUTH,
            404: EXIT_NOT_FOUND,
            405: EXIT_USAGE,
            413: EXIT_VALIDATION,
            429: EXIT_RATE_LIMIT,
        }.get(self.status, EXIT_SERVER if self.status >= 500 else EXIT_ERROR)

    def format(self) -> str:
        lines = [f"HTTP {self.status}: {self.message}"]

        if self.code:
            lines.append(f"  code: {self.code}")

        if self.index is not None:
            lines.append(f"  failing element: index {self.index}")

        for line in format_issues(self.issues):
            lines.append(f"  {line}")

        if isinstance(self.data, list) and self.data:
            lines.append(
                f"  {len(self.data)} item(s) were written before the failure "
                "(bulk writes are not transactional)"
            )

        return "\n".join(lines)


def format_issues(issues: Any) -> list[str]:
    """Render zod's `flatten()` output as `field: message` lines."""

    if not isinstance(issues, dict):
        return []

    lines: list[str] = []

    for message in issues.get("formErrors") or []:
        lines.append(str(message))

    field_errors = issues.get("fieldErrors")

    if isinstance(field_errors, dict):
        for field, messages in field_errors.items():
            joined = "; ".join(str(m) for m in (messages or []))
            lines.append(f"{field}: {joined}")

    return lines
