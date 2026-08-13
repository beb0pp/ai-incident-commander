"""Vector store port plus an in-memory implementation.

``InMemoryVectorStore`` is exact (brute-force cosine over every chunk), which is
the right call at runbook scale: a few hundred chunks search in under a
millisecond, and exact beats approximate when the corpus fits in RAM. A pgvector
implementation of the same protocol is the documented next step for a corpus
that outgrows the process — see ``docs/adr/0004``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from aic.rag.embeddings import EmbeddingProvider, cosine_similarity


@dataclass(frozen=True, slots=True)
class Chunk:
    """A retrievable slice of a document."""

    id: str
    document_id: str
    title: str
    text: str
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    chunk: Chunk
    score: float


class VectorStore(Protocol):
    def add(self, chunks: list[Chunk]) -> None: ...

    def search(self, query: str, *, limit: int = 5) -> list[ScoredChunk]: ...

    def __len__(self) -> int: ...


class InMemoryVectorStore:
    """Exact cosine search over an in-process corpus."""

    def __init__(self, embedder: EmbeddingProvider) -> None:
        self._embedder = embedder
        self._chunks: list[Chunk] = []
        self._vectors: list[list[float]] = []

    def add(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        vectors = self._embedder.embed([c.text for c in chunks])
        self._chunks.extend(chunks)
        self._vectors.extend(vectors)

    def search(self, query: str, *, limit: int = 5) -> list[ScoredChunk]:
        if not self._chunks or limit <= 0:
            return []
        (query_vector,) = self._embedder.embed([query])
        scored = [
            ScoredChunk(chunk=chunk, score=cosine_similarity(query_vector, vector))
            for chunk, vector in zip(self._chunks, self._vectors, strict=True)
        ]
        scored.sort(key=lambda s: (-s.score, s.chunk.id))
        return [s for s in scored[:limit] if s.score > 0.0]

    def __len__(self) -> int:
        return len(self._chunks)
