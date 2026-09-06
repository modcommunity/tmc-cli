"""Rendering results — for a human at a terminal and for the pipe after it.

Two audiences, one rule: anything a script might parse goes to **stdout**, and
everything else (progress, retry notices, pagination summaries) goes to
**stderr**. So `tmc mod list -o ids | xargs -n1 tmc mod get` works even while the
CLI is narrating a retry, and `2>/dev/null` never removes data.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
from typing import Any, Sequence

FORMATS = ("table", "json", "jsonl", "csv", "tsv", "ids", "yaml")

_ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
}


def use_color(stream: Any = None) -> bool:
    stream = stream or sys.stdout

    if os.environ.get("NO_COLOR") is not None:
        return False

    if os.environ.get("TMC_COLOR") == "always":
        return True

    return bool(getattr(stream, "isatty", lambda: False)())


def paint(text: str, *styles: str, stream: Any = None) -> str:
    if not use_color(stream):
        return text

    prefix = "".join(_ANSI[s] for s in styles if s in _ANSI)

    return f"{prefix}{text}{_ANSI['reset']}" if prefix else text


def info(message: str) -> None:
    print(paint(message, "dim", stream=sys.stderr), file=sys.stderr)


def success(message: str) -> None:
    print(paint(message, "green", stream=sys.stderr), file=sys.stderr)


def warn(message: str) -> None:
    print(paint(f"warning: {message}", "yellow", stream=sys.stderr), file=sys.stderr)


def error(message: str) -> None:
    print(paint(message, "red", stream=sys.stderr), file=sys.stderr)


# ---- Value formatting -------------------------------------------------------


def cell(value: Any, width: int = 48) -> str:
    """One table cell: compact, single-line, never wider than `width`."""

    if value is None:
        return "-"

    if isinstance(value, bool):
        return "yes" if value else "no"

    if isinstance(value, (list, tuple)):
        if not value:
            return "-"

        if all(isinstance(v, (str, int, float)) for v in value):
            text = ", ".join(str(v) for v in value)
        else:
            text = f"[{len(value)} items]"
    elif isinstance(value, dict):
        text = json.dumps(value, separators=(",", ":"))
    else:
        text = str(value)

    text = " ".join(text.split())

    return text if len(text) <= width else text[: width - 1] + "…"


def _rows(payload: Any) -> list[dict[str, Any]]:
    """Normalise whatever came back into a list of row-shaped dicts."""

    if payload is None:
        return []

    if isinstance(payload, dict):
        return [payload]

    if isinstance(payload, list):
        return [row if isinstance(row, dict) else {"value": row} for row in payload]

    return [{"value": payload}]


def _columns(rows: Sequence[dict[str, Any]], preferred: Sequence[str]) -> list[str]:
    """Preferred columns that actually appear, then anything else the rows have.

    Falling back to "everything else" matters because the row shapes come from
    Prisma, not from this CLI's mirror — a column added upstream shows up
    instead of vanishing because the default list is stale.
    """

    present = {key for row in rows for key in row.keys()}

    chosen = [name for name in preferred if name in present]

    if not chosen:
        # No preference matched: lead with id, then whatever is left, in the
        # order the first row declared them (Prisma's column order).
        ordered = list(rows[0].keys()) if rows else []
        extras = [key for key in ordered if key != "id"]
        chosen = (["id"] if "id" in present else []) + extras[:7]

    return chosen


def render(
    payload: Any,
    *,
    fmt: str = "table",
    columns: Sequence[str] | None = None,
    fields: Sequence[str] | None = None,
    stream: Any = None,
) -> None:
    """Write `payload` to stdout in the requested format."""

    stream = stream or sys.stdout

    if fields:
        payload = _project(payload, fields)

    if fmt == "json":
        json.dump(payload, stream, indent=2, default=str)
        stream.write("\n")
        return

    if fmt == "yaml":
        stream.write(_to_yaml(payload, 0))
        return

    rows = _rows(payload)

    if fmt == "jsonl":
        for row in rows:
            stream.write(json.dumps(row, default=str) + "\n")
        return

    if fmt == "ids":
        for row in rows:
            value = row.get("id", row.get("value"))

            if value is not None:
                stream.write(f"{value}\n")
        return

    if fmt in ("csv", "tsv"):
        if not rows:
            return

        names = list(fields) if fields else _columns(rows, columns or ())
        writer = csv.writer(
            stream, delimiter="\t" if fmt == "tsv" else ",", lineterminator="\n"
        )
        writer.writerow(names)

        for row in rows:
            writer.writerow(
                [
                    _scalar(row.get(name))
                    for name in names
                ]
            )
        return

    _render_table(rows, columns or (), fields, stream)


def _scalar(value: Any) -> str:
    """A CSV cell: scalars as-is, structures as compact JSON."""

    if value is None:
        return ""

    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), default=str)

    return str(value)


def _render_table(
    rows: Sequence[dict[str, Any]],
    preferred: Sequence[str],
    fields: Sequence[str] | None,
    stream: Any,
) -> None:
    if not rows:
        print(paint("(no results)", "dim", stream=stream), file=stream)
        return

    names = list(fields) if fields else _columns(rows, preferred)

    # A single row with many columns reads far better as a key/value block than
    # as one very wide line — this is the `tmc mod get 5` case.
    if len(rows) == 1 and len(names) > 6 and not fields:
        _render_detail(rows[0], stream)
        return

    table = [[cell(row.get(name)) for name in names] for row in rows]
    widths = [
        max(len(name), *(len(row[i]) for row in table)) if table else len(name)
        for i, name in enumerate(names)
    ]

    header = "  ".join(name.ljust(widths[i]) for i, name in enumerate(names))
    print(paint(header, "bold", stream=stream), file=stream)
    print(paint("  ".join("─" * w for w in widths), "dim", stream=stream), file=stream)

    for row in table:
        print("  ".join(value.ljust(widths[i]) for i, value in enumerate(row)), file=stream)


def _render_detail(row: dict[str, Any], stream: Any) -> None:
    width = max((len(k) for k in row), default=0)

    for key, value in row.items():
        label = paint(key.rjust(width), "cyan", stream=stream)
        print(f"{label}  {cell(value, width=120)}", file=stream)


def _project(payload: Any, fields: Sequence[str]) -> Any:
    """Keep only the named keys — `--field id,name` over a list or one object."""

    def pick(row: Any) -> Any:
        if not isinstance(row, dict):
            return row

        return {name: row.get(name) for name in fields}

    if isinstance(payload, list):
        return [pick(row) for row in payload]

    return pick(payload)


def _to_yaml(value: Any, indent: int) -> str:
    """A deliberately small YAML writer — enough for API payloads, no library.

    Only the shapes this API returns: scalars, lists and string-keyed maps.
    """

    pad = "  " * indent

    if isinstance(value, dict):
        if not value:
            return f"{pad}{{}}\n"

        out = io.StringIO()

        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                out.write(f"{pad}{key}:\n")
                out.write(_to_yaml(item, indent + 1))
            else:
                out.write(f"{pad}{key}: {_yaml_scalar(item)}\n")

        return out.getvalue()

    if isinstance(value, list):
        if not value:
            return f"{pad}[]\n"

        out = io.StringIO()

        for item in value:
            if isinstance(item, (dict, list)) and item:
                nested = _to_yaml(item, indent + 1)
                first, _, rest = nested.partition("\n")
                out.write(f"{pad}- {first.strip()}\n")

                if rest.strip():
                    out.write(rest if rest.endswith("\n") else rest + "\n")
            else:
                out.write(f"{pad}- {_yaml_scalar(item)}\n")

        return out.getvalue()

    return f"{pad}{_yaml_scalar(value)}\n"


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"

    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, (int, float)):
        return str(value)

    text = str(value)

    if text == "" or any(c in text for c in ":#\n\"'{}[]") or text.strip() != text:
        return json.dumps(text)

    return text


def human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"

        size /= 1024

    return f"{size:.1f} TB"
