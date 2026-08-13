"""Embedding port, plus a dependency-free implementation.

:class:`HashingEmbedding` is a real, deterministic embedder — hashed bag of
words with sublinear term frequency and L2 normalization, the classic
"hashing trick". It retrieves well on a corpus of a few dozen runbooks, needs no
model download, no API call, and no vector-DB service, which is what keeps this
repo clonable and its tests hermetic.

It is *not* a semantic embedder: it matches vocabulary, not meaning. Swapping in
a hosted embedding model is an implementation of this same protocol — see
``docs/adr/0004``.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Protocol

_TOKEN = re.compile(r"[a-z0-9_]+")

#: Tokens that carry no retrieval signal in operational documentation.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
        "have", "how", "if", "in", "into", "is", "it", "its", "of", "on", "or",
        "that", "the", "then", "there", "these", "this", "to", "was", "were",
        "what", "when", "which", "who", "will", "with", "you", "your",
    }
)  # fmt: skip


class EmbeddingProvider(Protocol):
    """Turns text into vectors. The only thing the vector store needs to know."""

    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, stopwords removed."""
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1]


class HashingEmbedding:
    """Deterministic sparse-to-dense embedding via the hashing trick."""

    def __init__(self, dimensions: int = 512) -> None:
        if dimensions < 16:
            raise ValueError("dimensions must be at least 16")
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        counts = Counter(tokenize(text))
        vector = [0.0] * self._dimensions
        for token, count in counts.items():
            # Sublinear TF keeps a term repeated 50 times from swamping the vector.
            weight = 1.0 + math.log(count)
            bucket = self._bucket(token)
            # A sign derived from a second hash keeps unrelated collisions from
            # always adding constructively.
            vector[bucket] += weight * self._sign(token)
        return _l2_normalize(vector)

    def _bucket(self, token: str) -> int:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % self._dimensions

    @staticmethod
    def _sign(token: str) -> float:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=1, salt=b"sign").digest()
        return 1.0 if digest[0] % 2 == 0 else -1.0


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return vector
    return [v / norm for v in vector]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two vectors, clamped to ``[0, 1]``.

    Both inputs are L2-normalized by construction, so this is a dot product.
    Negative similarity is clamped away: for retrieval ranking, "anti-correlated"
    and "unrelated" are the same answer.
    """
    if len(a) != len(b):
        raise ValueError(f"dimension mismatch: {len(a)} != {len(b)}")
    return max(0.0, min(1.0, sum(x * y for x, y in zip(a, b, strict=True))))
