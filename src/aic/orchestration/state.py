"""The state object that flows through the investigation graph.

Every node reads from it and writes to it, and it is fully serializable — which
is what makes checkpointing, resuming, and post-mortem inspection of an
investigation possible. If a node cannot express its output as a field here, it
is doing something the rest of the system cannot audit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from aic.domain.models import (
    ActionPlan,
    Anomaly,
    Hypothesis,
    Incident,
    InfrastructureFinding,
    RunbookMatch,
    Signal,
)


def _now() -> datetime:
    return datetime.now(UTC)


class NodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class NodeTrace(BaseModel):
    """Per-node execution record. This is the observability backbone."""

    model_config = ConfigDict(extra="forbid")

    name: str
    status: NodeStatus = NodeStatus.PENDING
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    @property
    def duration_ms(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds() * 1000.0


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, input_tokens: int, output_tokens: int, cache_read_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_tokens += cache_read_tokens


class InvestigationState(BaseModel):
    """Everything known about one incident investigation, at one point in time."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    incident: Incident

    # Inputs
    signals: list[Signal] = Field(default_factory=list)

    # Per-agent outputs
    anomalies: list[Anomaly] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    findings: list[InfrastructureFinding] = Field(default_factory=list)
    runbooks: list[RunbookMatch] = Field(default_factory=list)
    plan: ActionPlan | None = None

    # Execution metadata
    traces: list[NodeTrace] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    errors: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None

    # -- trace helpers ----------------------------------------------------

    def trace(self, name: str) -> NodeTrace:
        """Fetch (or create) the trace record for a node."""
        for existing in self.traces:
            if existing.name == name:
                return existing
        created = NodeTrace(name=name)
        self.traces.append(created)
        return created

    @property
    def failed_nodes(self) -> list[str]:
        return [t.name for t in self.traces if t.status is NodeStatus.FAILED]

    @property
    def succeeded(self) -> bool:
        return not self.failed_nodes

    # -- convenience ------------------------------------------------------

    def top_hypothesis(self) -> Hypothesis | None:
        return max(self.hypotheses, key=lambda h: h.confidence, default=None)

    def unhealthy_findings(self) -> list[InfrastructureFinding]:
        return [f for f in self.findings if not f.healthy]

    def snapshot(self) -> dict[str, Any]:
        """Serializable form, for checkpoints and API responses."""
        return self.model_dump(mode="json")

    @classmethod
    def restore(cls, payload: dict[str, Any]) -> Self:
        return cls.model_validate(payload)
