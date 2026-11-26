"""Crash points for recovery tests (D55).

`crash_point("after_approval_commit")` raises SimulatedCrash when KESTREL_CRASH_AT names
that point (comma-separated). SimulatedCrash derives from BaseException so the app's own
`except Exception` handlers can't swallow it: like a killed process, it ends the request
right there, after whatever was already committed.
"""

from __future__ import annotations

import os


class SimulatedCrash(BaseException):
    pass


def crash_point(name: str) -> None:
    if name in {p.strip() for p in os.environ.get("KESTREL_CRASH_AT", "").split(",") if p.strip()}:
        raise SimulatedCrash(name)
