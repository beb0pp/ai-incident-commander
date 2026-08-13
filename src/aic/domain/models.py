"""Core domain model.

These types are the contract between the agents, the orchestrator, the
persistence layer, and the HTTP API. They double as the JSON schemas the model
is forced to fill in (structured outputs), which is what keeps agent output
parseable instead of free-form prose.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class Severity(StrEnum):
    SEV1 = "sev1"
    SEV2 = "sev2"
    SEV3 = "sev3"
    SEV4 = "sev4"


class IncidentStatus(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_approval"
    MITIGATING = "mitigating"
    RESOLVED = "resolved"
    FAILED = "failed"


class SignalKind(StrEnum):
    LOG = "log"
    METRIC = "metric"
    EVENT = "event"
    TRACE = "trace"


class RiskLevel(StrEnum):
    """Blast radius of an action, ordered from harmless to destructive."""

    READ_ONLY = "read_only"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return _RISK_ORDER[self]


_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.READ_ONLY: 0,
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
}


class _Frozen(BaseModel):
    """Immutable value object. Agents may only produce new ones, never mutate."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ServiceRef(_Frozen):
    """A component the platform can reason about (an app, a queue, a database)."""

    name: str
    kind: Literal["service", "database", "cache", "queue", "loadbalancer"] = "service"
    environment: str = "prod"

    def __str__(self) -> str:
        return f"{self.name}@{self.environment}"


class Signal(_Frozen):
    """A normalized log line, metric sample, trace span, or platform event.

    Raw telemetry arrives in a dozen shapes; the Monitoring Agent flattens all of
    it into this one so downstream agents have a single format to reason about.
    """

    id: str = Field(default_factory=_new_id)
    kind: SignalKind
    service: ServiceRef
    timestamp: datetime
    message: str
    value: float | None = None
    labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _require_tz(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("signal timestamp must be timezone-aware")
        return v.astimezone(UTC)


class Anomaly(_Frozen):
    """A signal cluster the Monitoring Agent considers worth investigating."""

    id: str = Field(default_factory=_new_id)
    service: ServiceRef
    summary: str
    kind: SignalKind
    first_seen: datetime
    last_seen: datetime
    signal_ids: list[str] = Field(default_factory=list)
    severity_hint: Severity = Severity.SEV3
    score: Confidence = 0.5


class Evidence(_Frozen):
    """A pointer back to what a conclusion was actually based on.

    Every hypothesis and every proposed action must cite evidence. This is the
    mechanism that keeps output auditable rather than merely plausible-sounding.
    """

    source: Literal["signal", "anomaly", "tool", "runbook"]
    reference: str
    detail: str


class Hypothesis(_Frozen):
    """A candidate root cause produced by the Diagnostic Agent."""

    id: str = Field(default_factory=_new_id)
    title: str
    reasoning: str
    confidence: Confidence
    suspected_services: list[ServiceRef] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


class InfrastructureFinding(_Frozen):
    """A fact the Infrastructure Agent established by actually calling a tool."""

    id: str = Field(default_factory=_new_id)
    tool: str
    resource: str
    summary: str
    healthy: bool
    raw: dict[str, object] = Field(default_factory=dict)


class RunbookMatch(_Frozen):
    """A retrieved operational procedure plus why it was considered relevant."""

    document_id: str
    title: str
    excerpt: str
    score: Confidence
    steps: list[str] = Field(default_factory=list)


class ProposedAction(_Frozen):
    """A corrective step. Never executed without passing guardrails first."""

    id: str = Field(default_factory=_new_id)
    title: str
    description: str
    command: str | None = None
    target: ServiceRef | None = None
    risk: RiskLevel = RiskLevel.HIGH
    rationale: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    requires_approval: bool = True
    rollback: str | None = None


class ApprovalDecision(_Frozen):
    """The human half of the Human-in-the-Loop contract."""

    action_id: str
    approved: bool
    decided_by: str
    decided_at: datetime = Field(default_factory=_now)
    comment: str | None = None


class ActionPlan(_Frozen):
    """The consolidated output of an investigation."""

    id: str = Field(default_factory=_new_id)
    summary: str
    actions: list[ProposedAction] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)

    def action(self, action_id: str) -> ProposedAction | None:
        return next((a for a in self.actions if a.id == action_id), None)


class Incident(BaseModel):
    """Aggregate root. Mutable: it accumulates state as the investigation runs."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=_new_id)
    title: str
    description: str = ""
    severity: Severity = Severity.SEV3
    status: IncidentStatus = IncidentStatus.OPEN
    services: list[ServiceRef] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    plan: ActionPlan | None = None
    approvals: list[ApprovalDecision] = Field(default_factory=list)

    def transition_to(self, status: IncidentStatus) -> None:
        self.status = status
        self.updated_at = _now()

    def record_decision(self, decision: ApprovalDecision) -> None:
        """Last decision per action wins, so an operator can reverse themselves."""
        self.approvals = [a for a in self.approvals if a.action_id != decision.action_id]
        self.approvals.append(decision)
        self.updated_at = _now()

    def decision_for(self, action_id: str) -> ApprovalDecision | None:
        return next((a for a in self.approvals if a.action_id == action_id), None)
