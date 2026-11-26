"""Parse the synthetic policy corpus (Markdown with YAML front-matter) and chunk it."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from kestrel.access import KNOWN_GROUPS

REQUIRED = ("id", "title", "document_type", "department", "status", "audience")
# Brief: 400–800 tokens per chunk. ~4 characters per token for English policy text.
CHARS_PER_TOKEN = 4
MAX_CHARS = 2400   # ≈ 600 tokens
OVERLAP_CHARS = 300


@dataclass
class Document:
    document_id: str
    title: str
    source_path: str
    document_type: str
    department: str
    sensitivity: str
    effective_from: date | None
    status: str
    supersedes: str | None
    body: str
    audience: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()


@dataclass
class Chunk:
    chunk_id: str
    document_id: str
    chunk_index: int
    text: str
    token_count: int
    metadata: dict[str, Any]


def parse_document(path: Path, root: Path) -> Document:
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.S)
    if not m:
        raise ValueError(f"{path}: missing front-matter")
    meta = yaml.safe_load(m.group(1)) or {}
    missing = [k for k in REQUIRED if k not in meta]
    if missing:
        raise ValueError(f"{path}: missing front-matter keys {missing}")
    audience = meta["audience"]
    # Who may read it (D54). Required and checked, so a missing or misspelt group fails the
    # ingest instead of quietly exposing or hiding a document.
    if isinstance(audience, str):
        audience = [audience]
    if not audience or not isinstance(audience, list) or set(audience) - KNOWN_GROUPS:
        raise ValueError(f"{path}: audience must be a non-empty list of {sorted(KNOWN_GROUPS)}, got {audience!r}")
    return Document(
        audience=sorted(audience),
        document_id=meta["id"],
        title=meta["title"],
        source_path=str(path.relative_to(root)),
        document_type=meta["document_type"],
        department=meta["department"],
        sensitivity=meta.get("sensitivity", "internal"),
        effective_from=meta.get("effective_from"),
        status=meta["status"],
        supersedes=meta.get("supersedes"),
        body=m.group(2).strip(),
        meta=meta,
    )


def load_corpus(corpus_dir: Path) -> list[Document]:
    docs = [parse_document(p, corpus_dir) for p in sorted(corpus_dir.glob("*.md"))]
    ids = [d.document_id for d in docs]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate document ids: {sorted(dupes)}")
    return docs


def _sections(body: str) -> list[str]:
    """Split on level-2 headings so a chunk rarely cuts a rule in half."""
    parts = re.split(r"(?m)^(?=## )", body)
    return [p.strip() for p in parts if p.strip()]


def chunk_document(doc: Document) -> list[Chunk]:
    pieces: list[str] = []
    buf = ""
    for sec in _sections(doc.body):
        if len(buf) + len(sec) + 2 <= MAX_CHARS:
            buf = f"{buf}\n\n{sec}" if buf else sec
            continue
        if buf:
            pieces.append(buf)
        while len(sec) > MAX_CHARS:  # oversized section: hard split with overlap
            pieces.append(sec[:MAX_CHARS])
            sec = sec[MAX_CHARS - OVERLAP_CHARS:]
        buf = sec
    if buf:
        pieces.append(buf)

    base_meta = {
        "document_id": doc.document_id,
        "title": doc.title,
        "document_type": doc.document_type,
        "department": doc.department,
        "sensitivity": doc.sensitivity,
        "audience": doc.audience,
        "status": doc.status,
        "source_path": doc.source_path,
        "effective_from": doc.effective_from.isoformat() if doc.effective_from else None,
        "applies_to_txn_before": _iso(doc.meta.get("applies_to_txn_before")),
        "applies_to_txn_on_or_after": _iso(doc.meta.get("applies_to_txn_on_or_after")),
    }
    out = []
    for i, text in enumerate(pieces):
        # Prefix the title so every chunk is self-describing for retrieval and citation.
        full = f"{doc.title}\n\n{text}"
        out.append(Chunk(
            chunk_id=f"{doc.document_id}:{i}", document_id=doc.document_id, chunk_index=i,
            text=full, token_count=max(1, len(full) // CHARS_PER_TOKEN), metadata=dict(base_meta),
        ))
    return out


def _iso(v: Any) -> str | None:
    return v.isoformat() if isinstance(v, date) else v
