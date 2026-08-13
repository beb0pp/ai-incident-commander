"""Runbook retrieval, mapped onto the domain model."""

from __future__ import annotations

from aic.domain.models import RunbookMatch
from aic.rag.embeddings import tokenize
from aic.rag.indexer import extract_steps
from aic.rag.store import VectorStore

#: Below this cosine score a "match" is noise, and handing the model a noisy
#: runbook is worse than handing it none: it will try to follow it anyway.
DEFAULT_MIN_SCORE = 0.10

EXCERPT_CHARS = 600


class RunbookRetriever:
    """Turns an incident description into candidate operational procedures."""

    def __init__(
        self,
        store: VectorStore,
        *,
        limit: int = 4,
        min_score: float = DEFAULT_MIN_SCORE,
    ) -> None:
        self._store = store
        self._limit = limit
        self._min_score = min_score

    def retrieve(self, query: str) -> list[RunbookMatch]:
        # The embedder is lexical (hashed bag of words), so a chunk that shares no
        # vocabulary with the query cannot be a genuine match — any similarity it
        # scores is a hash collision. Requiring one real shared token removes that
        # entire class of false positive, which a score threshold alone does not:
        # collisions routinely land above any threshold low enough to be useful.
        query_tokens = set(tokenize(query))

        matches: list[RunbookMatch] = []
        for scored in self._store.search(query, limit=self._limit):
            if scored.score < self._min_score:
                continue
            text = scored.chunk.text
            if query_tokens and not query_tokens & set(tokenize(text)):
                continue
            matches.append(
                RunbookMatch(
                    document_id=scored.chunk.id,
                    title=scored.chunk.title,
                    excerpt=_excerpt(text),
                    score=round(scored.score, 4),
                    steps=extract_steps(text),
                )
            )
        return matches


def _excerpt(text: str) -> str:
    if len(text) <= EXCERPT_CHARS:
        return text
    return text[:EXCERPT_CHARS].rsplit(" ", 1)[0] + "…"
