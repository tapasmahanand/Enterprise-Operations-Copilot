"""Normalise BANKING77 into banking_queries with the brief's fields."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path
from typing import Iterator

import yaml

from kestrel.config import ROOT

INTENTS_PATH = ROOT / "rules" / "intents.yaml"


def load_intents(path: Path = INTENTS_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalise(banking77_dir: Path, intents: dict | None = None) -> Iterator[dict]:
    intents = intents or load_intents()
    scoped = intents["in_scope"]
    default = intents["defaults_out_of_scope"]
    for split in ("train", "test"):
        with open(banking77_dir / f"{split}.csv", encoding="utf-8") as f:
            for i, row in enumerate(csv.DictReader(f)):
                label = row["category"]
                spec = scoped.get(label, default)
                text = row["text"].strip()
                qid = f"{split}-{i:05d}-" + hashlib.sha1(text.encode()).hexdigest()[:8]
                yield {
                    "query_id": qid,
                    "split": split,
                    "user_query": text,
                    "intent_label": label,
                    "in_scope": label in scoped,
                    "priority": spec["priority"],
                    "department": spec["department"],
                    "risk_level": spec["risk_level"],
                }
