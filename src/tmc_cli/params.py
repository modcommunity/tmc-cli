"""Turning command-line words into a JSON body the strict schemas will accept.

The API's schemas do not coerce. `{"appId": "3"}` is a validation error, not a
number, and `{"hidden": "true"}` is not a boolean — so a CLI that passed strings
straight through would be a CLI whose every numeric flag is a 400. This module
is where `--set appId=3` becomes `3`, using the field's declared type from
`schema.py`.

THREE ASSIGNMENT FORMS
----------------------
    --set name="My Mod"          value coerced by the field's type
    --set-json redirect='{...}'  value parsed as JSON, whatever the field is
    --set-file content=BODY.md   value read from a file (a mod body is not
                                 something anyone wants to quote into a shell)

`--set field=` (empty) sends JSON `null`, which is how the API clears a nullable
field — distinct from omitting it, which leaves the stored value alone.
"""

from __future__ import annotations

import difflib
import json
import os
from typing import Any

from .errors import UsageError
from .schema import ENUMS, BOOL, DATE, FLOAT, INT, INT_LIST, JSON, STR_LIST, TypeSpec

TRUE_WORDS = {"1", "true", "yes", "y", "on"}
FALSE_WORDS = {"0", "false", "no", "n", "off"}


def parse_bool(raw: str, field_name: str = "value") -> bool:
    lowered = raw.strip().lower()

    if lowered in TRUE_WORDS:
        return True

    if lowered in FALSE_WORDS:
        return False

    raise UsageError(
        f"'{raw}' is not a boolean for {field_name} (use true/false)."
    )


def _split(assignment: str, flag: str) -> tuple[str, str]:
    if "=" not in assignment:
        raise UsageError(
            f"{flag} expects field=value, got '{assignment}'.",
        )

    name, _, value = assignment.partition("=")
    name = name.strip()

    if not name:
        raise UsageError(f"{flag} expects a field name before '='.")

    return name, value


def coerce(value: str, type_: str, field_name: str) -> Any:
    """Coerce one raw string into the field's declared JSON type."""

    # An empty value means null — the only way to clear a nullable column.
    if value == "":
        return None

    if type_ == INT:
        try:
            return int(value, 10)
        except ValueError:
            raise UsageError(f"{field_name} expects an integer, got '{value}'.") from None

    if type_ == FLOAT:
        try:
            return float(value)
        except ValueError:
            raise UsageError(f"{field_name} expects a number, got '{value}'.") from None

    if type_ == BOOL:
        return parse_bool(value, field_name)

    if type_ == STR_LIST:
        return [part.strip() for part in value.split(",") if part.strip()]

    if type_ == INT_LIST:
        out = []

        for part in value.split(","):
            part = part.strip()

            if not part:
                continue

            try:
                out.append(int(part, 10))
            except ValueError:
                raise UsageError(
                    f"{field_name} expects a comma-separated list of integers, "
                    f"got '{part}'."
                ) from None

        return out

    if type_ == JSON:
        try:
            return json.loads(value)
        except json.JSONDecodeError as err:
            raise UsageError(
                f"{field_name} expects JSON ({err}). Tip: --set-json {field_name}='[...]'."
            ) from None

    if type_ == DATE:
        # Passed through as typed: the server parses with `z.coerce.date()`, so
        # any ISO-8601 string it understands is fine and re-formatting here would
        # only add a way to be wrong.
        return value

    return value


def check_field(spec: TypeSpec | None, name: str, *, allow_unknown: bool) -> None:
    """Reject a field the type does not have, with the near-miss if there is one.

    The server would reject it too — the schemas are `.strict()` — but it would
    cost a request and answer with zod's flattened issues rather than "did you
    mean tags?".
    """

    if spec is None or spec.get(name) is not None:
        return

    if allow_unknown:
        return

    known = spec.field_names()
    close = difflib.get_close_matches(name, known, n=1, cutoff=0.7)

    hint = f" Did you mean '{close[0]}'?" if close else ""

    raise UsageError(
        f"'{spec.name}' has no field '{name}'.{hint}",
        hint=(
            f"Run 'tmc schema {spec.name}' for the full list, or pass "
            "--allow-unknown-fields to send it anyway."
        ),
    )


def build_payload(
    spec: TypeSpec | None,
    *,
    sets: list[str] | None = None,
    set_jsons: list[str] | None = None,
    set_files: list[str] | None = None,
    json_body: str | None = None,
    json_file: str | None = None,
    allow_unknown: bool = False,
) -> dict[str, Any]:
    """Fold every body-shaping flag into one payload.

    Order is deliberate: a whole-body `--json` is the base, and individual
    `--set`s are applied on top. That way a stored template can be tweaked at
    the call site without editing the file.
    """

    payload: dict[str, Any] = {}

    if json_file:
        payload.update(_require_object(load_json_file(json_file), json_file))

    if json_body:
        payload.update(_require_object(parse_json_arg(json_body), "--json"))

    for assignment in sets or []:
        name, raw = _split(assignment, "--set")
        check_field(spec, name, allow_unknown=allow_unknown)

        field = spec.get(name) if spec else None
        value = coerce(raw, field.type if field else "str", name)

        if field and field.enum and value is not None:
            _check_enum(name, value, field.enum)

        payload[name] = value

    for assignment in set_jsons or []:
        name, raw = _split(assignment, "--set-json")
        check_field(spec, name, allow_unknown=allow_unknown)

        payload[name] = coerce(raw, JSON, name)

    for assignment in set_files or []:
        name, path = _split(assignment, "--set-file")
        check_field(spec, name, allow_unknown=allow_unknown)

        payload[name] = read_text_file(path)

    return payload


def _check_enum(field_name: str, value: Any, enum_name: str) -> None:
    allowed = ENUMS.get(enum_name, ())

    if not allowed or value in allowed:
        return

    close = difflib.get_close_matches(str(value).upper(), list(allowed), n=1, cutoff=0.6)
    hint = f" Did you mean '{close[0]}'?" if close else ""

    raise UsageError(
        f"{field_name} must be one of {', '.join(allowed)} — got '{value}'.{hint}"
    )


def parse_json_arg(raw: str) -> Any:
    """Parse `--json`, accepting `@path` and `-` (stdin) as well as literal JSON."""

    if raw == "-":
        import sys

        return _decode(sys.stdin.read(), "stdin")

    if raw.startswith("@"):
        return load_json_file(raw[1:])

    return _decode(raw, "--json")


def load_json_file(path: str) -> Any:
    if path == "-":
        import sys

        return _decode(sys.stdin.read(), "stdin")

    expanded = os.path.expanduser(path)

    try:
        with open(expanded, "r", encoding="utf-8") as handle:
            return _decode(handle.read(), expanded)
    except OSError as err:
        raise UsageError(f"Cannot read '{expanded}': {err}") from None


def read_text_file(path: str) -> str:
    if path == "-":
        import sys

        return sys.stdin.read()

    expanded = os.path.expanduser(path)

    try:
        with open(expanded, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as err:
        raise UsageError(f"Cannot read '{expanded}': {err}") from None


def _decode(text: str, source: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as err:
        raise UsageError(f"{source} is not valid JSON: {err}") from None


def _require_object(value: Any, source: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageError(f"{source} must contain a JSON object, not a {type(value).__name__}.")

    return value


def require_list(value: Any, source: str) -> list[Any]:
    """Unwrap a bare array or `{ "data": [...] }` — both shapes the API accepts."""

    if isinstance(value, dict) and isinstance(value.get("data"), list):
        return value["data"]

    if not isinstance(value, list):
        raise UsageError(f"{source} must contain a JSON array (or {{\"data\": [...]}}).")

    return value
