# ADR 0002 — A hand-written DAG engine instead of an agent framework

**Status:** Accepted · **Date:** 2026-08-13

## Context

The investigation is a five-node graph: monitoring → diagnostic → (infrastructure
and runbook, in parallel) → action. It needs dependency ordering, concurrency for
the two independent branches, per-node retries, degradation when an optional node
fails, per-node timeouts, and checkpointing.

LangGraph provides all of this. The question was not capability but ownership.

## Decision

Implement the engine in `aic/orchestration/graph.py` — roughly 150 lines.

The deciding argument: **the orchestration semantics are the interesting part of
an agent platform.** Retry policy, failure containment, and checkpoint placement
are design decisions. Behind a framework they become configuration — and stop
being visible, or directly testable, as engineering.

Three secondary considerations:

- **Exact semantics.** "An optional node's failure degrades the investigation and
  skips its dependents, but a required node's failure aborts the run" is a domain
  rule. Writing it directly is clearer than expressing it in a framework's
  vocabulary.
- **Test surface.** `tests/unit/test_graph.py` asserts cycle detection,
  concurrency (with a wall-clock assertion that fails if the branches ever
  serialize), retry counts, and skip propagation. Those test *our* logic, which
  is what makes them worth having.
- **Dependency weight.** LangGraph pulls in a large transitive tree for a
  five-node graph.

## Consequences

**Accepted:** we own the engine and its bugs. Anything a framework would give for
free — mid-graph human interrupts, time travel, a visual debugger, streaming — is
ours to build if we ever need it.

**Accepted:** "LangGraph" does not appear in this repository's dependencies, which
is a real cost under keyword-driven screening. The README and this ADR state the
reasoning so the choice reads as deliberate rather than uninformed.

**Gained:** the parallelism is real and proven, not assumed.

**Gained:** no framework upgrade can silently change how a production incident
pipeline behaves.

## Revisit if

The graph acquires cycles (an investigate → act → re-investigate loop), mid-graph
human interrupts, or cross-process distribution. At that point a framework's
semantics are worth more than our own.
