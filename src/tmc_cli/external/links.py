"""Which program owns `nxm://`, `curseforge://`, ...? Read only. Mirrors `scheme.rs`.

The CLI never registers or takes a scheme: that is the app's opt-in screen
(`external_scheme_set`), and another app's handler is never overwritten.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Callable

MANAGED = ("nxm", "curseforge", "modrinth", "ror2mm")

#: The app's bundle id and product name; either in a handler means "ours".
OWN_MARKERS = ("com.moddingcommunity.app", "gamespice")

Runner = Callable[[list[str]], "str | None"]


def system_runner(argv: list[str]) -> str | None:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None

    return out.stdout if out.returncode == 0 else None


def status(scheme: str, run: Runner = system_runner, platform: str | None = None) -> dict[str, str]:
    """`state` is registered, free or unknown; `by` names the handler when there is one."""

    plat = platform or sys.platform

    if plat == "darwin":
        return {"scheme": scheme, "state": "unknown", "by": "", "ours": "no"}

    if plat.startswith("win"):
        out = run(["reg", "query", f"HKCU\\Software\\Classes\\{scheme}\\shell\\open\\command", "/ve"])

        if out is None:
            return {"scheme": scheme, "state": "free", "by": "", "ours": "no"}

        cmd = ""

        for line in out.splitlines():
            if "REG_SZ" in line:
                cmd = line.split("REG_SZ", 1)[1].strip()

        return _registered(scheme, cmd) if cmd else {"scheme": scheme, "state": "unknown", "by": "", "ours": "no"}

    out = run(["xdg-mime", "query", "default", f"x-scheme-handler/{scheme}"])

    if out is None:
        return {"scheme": scheme, "state": "unknown", "by": "", "ours": "no"}

    handler = out.strip()

    return _registered(scheme, handler) if handler else {"scheme": scheme, "state": "free", "by": "", "ours": "no"}


def _registered(scheme: str, by: str) -> dict[str, str]:
    ours = any(m in by.lower() for m in OWN_MARKERS)

    return {"scheme": scheme, "state": "registered", "by": by, "ours": "yes" if ours else "no"}


def all_status(run: Runner = system_runner, platform: str | None = None) -> list[dict[str, str]]:
    return [status(s, run, platform) for s in MANAGED]
