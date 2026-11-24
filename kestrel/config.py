"""Settings from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    database_url: str
    openai_api_key: str | None
    embedding_provider: str      # openai | fake
    embedding_model: str
    embedding_dim: int
    corpus_dir: Path
    banking77_dir: Path
    enron_sample: Path


def get_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", "postgresql://kestrel:kestrel@localhost:5432/kestrel"),
        openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
        embedding_provider=os.environ.get("EMBEDDING_PROVIDER", "openai"),
        embedding_model=os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
        embedding_dim=int(os.environ.get("EMBEDDING_DIM", "1536")),
        corpus_dir=Path(os.environ.get("CORPUS_DIR", ROOT / "data" / "corpus")),
        banking77_dir=Path(os.environ.get("BANKING77_DIR", ROOT / "data" / "banking77")),
        enron_sample=Path(os.environ.get("ENRON_SAMPLE", ROOT / "data" / "enron" / "enron_sample.jsonl.gz")),
    )
