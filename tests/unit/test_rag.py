"""Embeddings, chunking, and retrieval."""

from __future__ import annotations

import pytest

from aic.config import Settings
from aic.rag.embeddings import HashingEmbedding, cosine_similarity, tokenize
from aic.rag.indexer import chunk_markdown, extract_steps, index_directory
from aic.rag.retriever import RunbookRetriever
from aic.rag.store import InMemoryVectorStore

SAMPLE = """# Redis Eviction Storm

Intro paragraph about the cluster.

## Symptoms

- Evictions is non-zero and rising
- Memory usage near 100 percent

## Mitigation

1. Shed the largest key space
2. Restore TTLs
"""


class TestEmbedding:
    def test_is_deterministic(self) -> None:
        embedder = HashingEmbedding(dimensions=128)
        assert embedder.embed(["connection pool"]) == embedder.embed(["connection pool"])

    def test_vectors_are_normalized(self) -> None:
        (vector,) = HashingEmbedding(dimensions=128).embed(["database connection exhaustion"])
        assert sum(v * v for v in vector) == pytest.approx(1.0, abs=1e-9)

    def test_similar_text_scores_above_unrelated_text(self) -> None:
        embedder = HashingEmbedding(dimensions=512)
        query, related, unrelated = embedder.embed(
            [
                "database connection pool exhausted",
                "the connection pool for the database is exhausted",
                "redis evictions memory pressure cache",
            ]
        )
        assert cosine_similarity(query, related) > cosine_similarity(query, unrelated)

    def test_empty_text_yields_a_zero_vector(self) -> None:
        (vector,) = HashingEmbedding(dimensions=64).embed([""])
        assert not any(vector)

    def test_stopwords_are_dropped(self) -> None:
        assert tokenize("the connection is in the pool") == ["connection", "pool"]

    def test_dimension_mismatch_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="dimension mismatch"):
            cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])


class TestChunking:
    def test_splits_on_section_headings(self) -> None:
        chunks = chunk_markdown("redis", SAMPLE)
        sections = [c.metadata["section"] for c in chunks]
        assert sections == ["Overview", "Symptoms", "Mitigation"]

    def test_document_title_is_embedded_in_every_chunk(self) -> None:
        """So a query naming the subject matches sections that never repeat it."""
        for chunk in chunk_markdown("redis", SAMPLE):
            assert "Redis Eviction Storm" in chunk.text

    def test_chunk_ids_are_stable_and_unique(self) -> None:
        ids = [c.id for c in chunk_markdown("redis", SAMPLE)]
        assert ids == ["redis#0", "redis#1", "redis#2"]

    def test_extracts_both_numbered_and_bulleted_steps(self) -> None:
        steps = extract_steps(SAMPLE)
        assert "Shed the largest key space" in steps
        assert "Evictions is non-zero and rising" in steps

    def test_empty_document_yields_no_chunks(self) -> None:
        assert chunk_markdown("empty", "") == []


class TestRetrieval:
    @pytest.fixture
    def retriever(self) -> RunbookRetriever:
        settings = Settings()
        store = InMemoryVectorStore(HashingEmbedding(dimensions=512))
        index_directory(store, settings.runbook_directory)
        return RunbookRetriever(store)

    def test_the_bundled_corpus_indexes(self) -> None:
        store = InMemoryVectorStore(HashingEmbedding(dimensions=512))
        count = index_directory(store, Settings().runbook_directory)
        assert count > 0
        assert len(store) == count

    def test_connection_query_retrieves_the_connection_runbook(
        self, retriever: RunbookRetriever
    ) -> None:
        matches = retriever.retrieve("timeout acquiring connection from pool max_connections")
        assert matches
        assert "Connection Pool Exhaustion" in matches[0].title

    def test_queue_query_retrieves_the_queue_runbook(self, retriever: RunbookRetriever) -> None:
        matches = retriever.retrieve("sqs queue backlog oldest message age consumers")
        assert matches
        assert "Backlog" in matches[0].title

    def test_matches_carry_steps_and_a_bounded_excerpt(
        self, retriever: RunbookRetriever
    ) -> None:
        matches = retriever.retrieve("database connection pool exhaustion mitigation rollback")
        assert any(m.steps for m in matches)
        assert all(len(m.excerpt) <= 610 for m in matches)

    def test_scores_are_descending(self, retriever: RunbookRetriever) -> None:
        matches = retriever.retrieve("deployment rollback ecs task definition")
        assert [m.score for m in matches] == sorted((m.score for m in matches), reverse=True)

    def test_missing_directory_is_a_clear_error(self) -> None:
        store = InMemoryVectorStore(HashingEmbedding())
        with pytest.raises(FileNotFoundError, match="runbook directory"):
            index_directory(store, "does/not/exist")
