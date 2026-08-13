# ADR 0004 — In-process RAG over a vector database

**Status:** Accepted · **Date:** 2026-08-13

## Context

The Runbook Agent needs to retrieve operational procedures relevant to an
incident. The corpus is an organization's runbooks: tens to low hundreds of
documents, a few hundred chunks, changing on a documentation cadence rather than
a request cadence.

The default architecture for this is a hosted embedding model plus a vector
database. For this corpus size, both are overkill — and both make the repository
impossible to clone and run.

## Decision

Three protocols with in-process implementations:

- `EmbeddingProvider` → `HashingEmbedding`. The hashing trick: hashed bag of
  words, sublinear term frequency, signed buckets, L2 normalization. Real
  information retrieval, deterministic, no model download, no API call.
- `VectorStore` → `InMemoryVectorStore`. Brute-force cosine over every chunk.
  At a few hundred chunks this is sub-millisecond, and exact beats approximate
  whenever the corpus fits in memory.
- Chunking on `##` section boundaries rather than a fixed token window.
  Operational documents are already organized into Symptoms / Diagnosis /
  Mitigation sections; retrieving half a procedure is worse than retrieving none.

Two things follow from `HashingEmbedding` being lexical rather than semantic, and
both are deliberate:

- **The relevance filter is a separate LLM pass.** Retrieval proposes,
  the model disposes. A procedure that shares vocabulary with the incident but
  addresses a different failure mode is worse than no procedure at all, because a
  responder under pressure will follow it.
- **A retrieved chunk must share at least one real token with the query.** A
  score threshold alone does not remove hash-collision false positives: collisions
  routinely land above any threshold low enough to be useful. Requiring genuine
  vocabulary overlap eliminates the entire class, and it is a correct invariant
  precisely *because* the embedder is lexical.

## Consequences

**Accepted:** retrieval is vocabulary-based. A runbook that says "connection
ceiling" will not match a query that says "too many sessions". The LLM relevance
pass compensates only for false positives, not false negatives.

**Accepted:** the index is rebuilt per process at startup. At this corpus size
that is milliseconds; it would not be at 100k documents.

**Gained:** `pytest` needs no service, no API key, and no network, and the
retrieval tests assert on real ranking behaviour rather than on a mock.

**Gained:** the corpus lives in `docs/runbooks/` as ordinary markdown, so it is
reviewable in a pull request.

## Revisit if

The corpus outgrows the process, or recall on paraphrased queries becomes the
limiting factor. Both point at the same migration: a hosted embedding model
behind `EmbeddingProvider` and pgvector behind `VectorStore`. Postgres is already
a dependency, so that is an implementation swap rather than a new service.
