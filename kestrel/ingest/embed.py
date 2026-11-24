"""Embedding providers. `openai` in the real stack; `fake` for tests and offline runs.

The fake provider hashes word unigrams and bigrams into a fixed-size vector. It is
lexical, not semantic, but deterministic, so pipeline and retrieval tests run without
network access or an API key.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol, Sequence

from kestrel.config import Settings

_WORD = re.compile(r"[a-z0-9₹]+")


class Embedder(Protocol):
    dim: int
    model: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class FakeEmbedder:
    model = "fake-hash-v1"

    def __init__(self, dim: int = 1536):
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        words = _WORD.findall(text.lower())
        feats = words + [f"{a}_{b}" for a, b in zip(words, words[1:])]
        v = [0.0] * self.dim
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
            v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class OpenAIEmbedder:
    def __init__(self, api_key: str, model: str, dim: int, batch_size: int = 64):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, max_retries=5)
        self.model, self.dim, self.batch_size = model, dim, batch_size

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            resp = self.client.embeddings.create(model=self.model, input=list(texts[i:i + self.batch_size]))
            out.extend(d.embedding for d in resp.data)
        return out


def make_embedder(s: Settings) -> Embedder:
    if s.embedding_provider == "fake":
        return FakeEmbedder(s.embedding_dim)
    if not s.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set (or set EMBEDDING_PROVIDER=fake for an offline run)")
    return OpenAIEmbedder(s.openai_api_key, s.embedding_model, s.embedding_dim)
