"""A small dependency-driven execution engine for agent graphs.

Roughly 150 lines buys the four properties an incident pipeline actually needs:

* **Declared dependencies.** Nodes state what they need; the engine derives the
  order and detects cycles at construction time rather than at 3am.
* **Automatic parallelism.** Nodes at the same depth run concurrently. The
  Infrastructure and Runbook agents both depend only on diagnosis, so they run
  side by side without anyone hand-wiring an ``asyncio.gather``.
* **Per-node retries and optionality.** A flaky tool integration degrades the
  investigation instead of aborting it.
* **Checkpointing.** State is persisted after every level, so a crashed run is
  inspectable and resumable.

See ``docs/adr/0002`` for why this is hand-written rather than a graph framework.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

from aic.orchestration.checkpoint import CheckpointStore
from aic.orchestration.state import InvestigationState, NodeStatus

log = structlog.get_logger(__name__)

NodeFn = Callable[[InvestigationState], Awaitable[None]]


class GraphDefinitionError(Exception):
    """The graph is malformed: a cycle, a dangling dependency, a duplicate name."""


@dataclass(frozen=True, slots=True)
class Node:
    """One unit of work in the investigation."""

    name: str
    run: NodeFn
    depends_on: tuple[str, ...] = ()
    retries: int = 0
    retry_backoff_seconds: float = 0.5
    #: An optional node's failure is recorded but does not abort the graph.
    optional: bool = False
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class Graph:
    """A validated DAG of nodes, executed level by level."""

    nodes: tuple[Node, ...]
    levels: tuple[tuple[Node, ...], ...] = field(repr=False)

    @classmethod
    def build(cls, nodes: Iterable[Node]) -> Graph:
        ordered = tuple(nodes)
        by_name: dict[str, Node] = {}
        for node in ordered:
            if node.name in by_name:
                raise GraphDefinitionError(f"duplicate node name: {node.name!r}")
            by_name[node.name] = node

        for node in ordered:
            for dependency in node.depends_on:
                if dependency not in by_name:
                    raise GraphDefinitionError(
                        f"node {node.name!r} depends on unknown node {dependency!r}"
                    )

        return cls(nodes=ordered, levels=_topological_levels(ordered, by_name))

    async def run(
        self,
        state: InvestigationState,
        *,
        checkpoints: CheckpointStore | None = None,
        timeout_seconds: float | None = None,
    ) -> InvestigationState:
        """Execute the graph, mutating and returning ``state``."""
        started = time.monotonic()

        for level in self.levels:
            runnable = [n for n in level if _dependencies_satisfied(n, state)]
            for skipped in (n for n in level if n not in runnable):
                trace = state.trace(skipped.name)
                trace.status = NodeStatus.SKIPPED
                trace.error = "upstream dependency failed"
                log.info("node.skipped", node=skipped.name, run_id=state.run_id)

            blocking = False
            if runnable:
                results = await asyncio.gather(*(self._run_node(n, state) for n in runnable))
                blocking = any(
                    not node.optional for node, ok in zip(runnable, results, strict=True) if not ok
                )

            if checkpoints is not None:
                await checkpoints.save(state)

            if blocking:
                log.warning("graph.aborted", run_id=state.run_id, failed=state.failed_nodes)
                break

            if timeout_seconds is not None and time.monotonic() - started > timeout_seconds:
                state.errors.append(f"investigation exceeded {timeout_seconds}s budget")
                log.warning("graph.timeout", run_id=state.run_id)
                break

        state.finished_at = datetime.now(UTC)
        if checkpoints is not None:
            await checkpoints.save(state)
        return state

    async def _run_node(self, node: Node, state: InvestigationState) -> bool:
        """Run one node with its retry budget. Returns whether it succeeded."""
        trace = state.trace(node.name)
        trace.status = NodeStatus.RUNNING
        trace.started_at = datetime.now(UTC)

        last_error: Exception | None = None
        for attempt in range(node.retries + 1):
            trace.attempts = attempt + 1
            try:
                coro = node.run(state)
                if node.timeout_seconds is not None:
                    await asyncio.wait_for(coro, timeout=node.timeout_seconds)
                else:
                    await coro
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                log.warning(
                    "node.attempt_failed",
                    node=node.name,
                    attempt=trace.attempts,
                    error=str(exc),
                    run_id=state.run_id,
                )
                if attempt < node.retries:
                    await asyncio.sleep(node.retry_backoff_seconds * (2**attempt))
                continue
            else:
                trace.status = NodeStatus.SUCCEEDED
                trace.finished_at = datetime.now(UTC)
                log.info(
                    "node.succeeded",
                    node=node.name,
                    attempts=trace.attempts,
                    duration_ms=trace.duration_ms,
                    run_id=state.run_id,
                )
                return True

        trace.status = NodeStatus.FAILED
        trace.finished_at = datetime.now(UTC)
        trace.error = str(last_error)
        state.errors.append(f"{node.name}: {last_error}")
        log.error("node.failed", node=node.name, error=str(last_error), run_id=state.run_id)
        return False


def _dependencies_satisfied(node: Node, state: InvestigationState) -> bool:
    for dependency in node.depends_on:
        trace = state.trace(dependency)
        if trace.status not in (NodeStatus.SUCCEEDED, NodeStatus.SKIPPED):
            return False
    return True


def _topological_levels(
    nodes: Sequence[Node], by_name: dict[str, Node]
) -> tuple[tuple[Node, ...], ...]:
    """Group nodes into levels where every node depends only on earlier levels."""
    remaining = {n.name: set(n.depends_on) for n in nodes}
    levels: list[tuple[Node, ...]] = []
    done: set[str] = set()

    while remaining:
        ready = sorted(name for name, deps in remaining.items() if deps <= done)
        if not ready:
            raise GraphDefinitionError(
                f"cycle detected among nodes: {sorted(remaining)}"
            )
        levels.append(tuple(by_name[name] for name in ready))
        done.update(ready)
        for name in ready:
            del remaining[name]

    return tuple(levels)
