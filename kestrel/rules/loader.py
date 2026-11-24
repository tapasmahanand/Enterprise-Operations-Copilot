"""Load the machine-readable rules file (rules/kestrel_rules.yaml)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "rules" / "kestrel_rules.yaml"


@dataclass(frozen=True)
class Calendar:
    closed_weekdays: frozenset[int]          # Monday=0 … Sunday=6
    closed_saturdays_of_month: frozenset[int]
    holidays: frozenset[date]

    def is_working_day(self, d: date) -> bool:
        if d.weekday() in self.closed_weekdays or d in self.holidays:
            return False
        if d.weekday() == 5 and ((d.day - 1) // 7 + 1) in self.closed_saturdays_of_month:
            return False
        return True


@dataclass(frozen=True)
class Rules:
    version: str
    calendar: Calendar
    raw: dict[str, Any]

    def regime_for(self, txn_date: date) -> dict[str, Any]:
        for regime in self.raw["liability_regimes"]:
            before = regime.get("applies_to_txn_before")
            on_or_after = regime.get("applies_to_txn_on_or_after")
            if before is not None and txn_date < before:
                return regime
            if on_or_after is not None and txn_date >= on_or_after:
                return regime
        raise ValueError(f"no liability regime covers {txn_date}")

    @property
    def kestrel_policy(self) -> dict[str, Any]:
        return self.raw["kestrel_policy"]

    @property
    def failed_txn_tat(self) -> dict[str, Any]:
        return self.raw["failed_txn_tat"]

    @property
    def approval_matrix(self) -> dict[str, Any]:
        return self.raw["approval_matrix"]


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _parse(raw: dict[str, Any]) -> Rules:
    cal = raw["calendar"]
    calendar = Calendar(
        closed_weekdays=frozenset(_WEEKDAYS.index(d) for d in cal["closed_weekdays"]),
        closed_saturdays_of_month=frozenset(cal["closed_saturdays_of_month"]),
        holidays=frozenset(cal["holidays"]),
    )
    return Rules(version=str(raw["version"]), calendar=calendar, raw=raw)


@lru_cache(maxsize=4)
def load_rules(path: str | Path = DEFAULT_PATH) -> Rules:
    with open(path, encoding="utf-8") as f:
        return _parse(yaml.safe_load(f))
