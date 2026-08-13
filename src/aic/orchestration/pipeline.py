"""The incident investigation pipeline.

::

    monitoring ──▶ diagnostic ──┬──▶ infrastructure ──┐
                                │                     ├──▶ action
                                └──▶ runbook ─────────┘

``infrastructure`` and ``runbook`` depend only on ``diagnostic``, so the engine
runs them concurrently — the tool-calling round trips and the retrieval pass
overlap instead of queueing behind each other.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog

from aic.agents.action import ActionAgent
from aic.agents.base import Agent
from aic.agents.diagnostic import DiagnosticAgent
from aic.agents.infrastructure import InfrastructureAgent
from aic.agents.monitoring import MonitoringAgent
from aic.agents.runbook import RunbookAgent
from aic.domain.models import Incident, IncidentStatus, Signal
from aic.infrastructure.observability import metrics
from aic.orchestration.checkpoint import CheckpointStore
from aic.orchestration.graph import Graph, Node
from aic.orchestration.state import InvestigationState, NodeStatus

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Agents:
    """The five agents, already wired to their dependencies."""

    monitoring: MonitoringAgent
    diagnostic: DiagnosticAgent
    infrastructure: InfrastructureAgent
    runbook: RunbookAgent
    action: ActionAgent


def _node(agent: Agent, *, retries: int = 1, timeout_seconds: float = 120.0) -> Node:
    """Wrap an agent as a graph node, reading its declared dependencies."""
    return Node(
        name=agent.name,
        run=agent.run,
        depends_on=agent.depends_on,
        retries=retries,
        optional=agent.optional,
        timeout_seconds=timeout_seconds,
    )


def build_graph(agents: Agents) -> Graph:
    """Assemble the investigation DAG."""
    return Graph.build(
        [
            _node(agents.monitoring, retries=1),
            _node(agents.diagnostic, retries=2),
            # Tool loops are the slowest step; give them room.
            _node(agents.infrastructure, retries=1, timeout_seconds=180.0),
            _node(agents.runbook, retries=1),
            _node(agents.action, retries=2),
        ]
    )


class InvestigationRunner:
    """Runs one incident through the graph and reports the outcome."""

    def __init__(
        self,
        graph: Graph,
        *,
        checkpoints: CheckpointStore | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._graph = graph
        self._checkpoints = checkpoints
        self._timeout = timeout_seconds

    async def run(self, incident: Incident, signals: list[Signal]) -> InvestigationState:
        state = InvestigationState(
            run_id=uuid.uuid4().hex,
            incident=incident,
            signals=signals,
        )
        incident.transition_to(IncidentStatus.INVESTIGATING)
        log.info(
            "investigation.started",
            run_id=state.run_id,
            incident_id=incident.id,
            signals=len(signals),
        )

        await self._graph.run(
            state, checkpoints=self._checkpoints, timeout_seconds=self._timeout
        )

        if not state.succeeded and state.plan is None:
            # Nothing actionable came out of the run; say so rather than leaving
            # the incident sitting in INVESTIGATING forever.
            incident.transition_to(IncidentStatus.FAILED)

        _record_metrics(state)

        log.info(
            "investigation.finished",
            run_id=state.run_id,
            status=incident.status,
            failed_nodes=state.failed_nodes,
            input_tokens=state.usage.input_tokens,
            output_tokens=state.usage.output_tokens,
        )
        return state


def _record_metrics(state: InvestigationState) -> None:
    """Project the run's traces onto the Prometheus registry."""
    metrics.investigations_total.labels(status=str(state.incident.status)).inc()

    if state.finished_at is not None:
        elapsed = (state.finished_at - state.started_at).total_seconds()
        metrics.investigation_duration_seconds.observe(elapsed)

    for trace in state.traces:
        metrics.agent_runs_total.labels(agent=trace.name, outcome=str(trace.status)).inc()
        if trace.duration_ms is not None and trace.status is NodeStatus.SUCCEEDED:
            metrics.agent_duration_seconds.labels(agent=trace.name).observe(
                trace.duration_ms / 1000.0
            )

    metrics.llm_tokens_total.labels(direction="input").inc(state.usage.input_tokens)
    metrics.llm_tokens_total.labels(direction="output").inc(state.usage.output_tokens)
    metrics.llm_tokens_total.labels(direction="cache_read").inc(state.usage.cache_read_tokens)

    if state.plan is not None:
        for action in state.plan.actions:
            if action.requires_approval:
                metrics.guardrail_events_total.labels(kind="approval_required").inc()
