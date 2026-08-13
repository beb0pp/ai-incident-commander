"""HTTP request and response models.

Kept separate from the domain models on purpose: the wire format is a published
contract with its own compatibility obligations, and the domain should be free to
change shape without breaking clients.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from aic.domain.models import (
    ActionPlan,
    Anomaly,
    Hypothesis,
    Incident,
    IncidentStatus,
    InfrastructureFinding,
    RunbookMatch,
    ServiceRef,
    Severity,
    Signal,
    SignalKind,
)
from aic.orchestration.state import InvestigationState, NodeTrace


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SignalPayload(_Model):
    """Raw telemetry as an ingestion client sends it."""

    kind: SignalKind
    service: str = Field(description="Service name, e.g. 'checkout-api'.")
    environment: str = "prod"
    timestamp: datetime
    message: str
    value: float | None = None
    labels: dict[str, str] = Field(default_factory=dict)

    def to_domain(self) -> Signal:
        return Signal(
            kind=self.kind,
            service=ServiceRef(name=self.service, environment=self.environment),
            timestamp=self.timestamp,
            message=self.message,
            value=self.value,
            labels=self.labels,
        )


class CreateIncidentRequest(_Model):
    title: str = Field(min_length=3, max_length=300)
    description: str = ""
    severity: Severity = Severity.SEV3
    services: list[str] = Field(default_factory=list)
    environment: str = "prod"
    #: Supplying signals here runs the investigation as part of the same request.
    signals: list[SignalPayload] = Field(default_factory=list)

    def service_refs(self) -> list[ServiceRef]:
        return [ServiceRef(name=name, environment=self.environment) for name in self.services]


class InvestigateRequest(_Model):
    signals: list[SignalPayload] = Field(min_length=1)


class DecisionRequest(_Model):
    approved: bool
    decided_by: str = Field(min_length=1, max_length=200)
    comment: str | None = Field(default=None, max_length=2000)


class IncidentResponse(_Model):
    id: str
    title: str
    description: str
    severity: Severity
    status: IncidentStatus
    services: list[ServiceRef]
    created_at: datetime
    updated_at: datetime
    plan: ActionPlan | None
    pending_approvals: list[str] = Field(
        description="Ids of actions still waiting on a human decision."
    )

    @classmethod
    def from_domain(cls, incident: Incident) -> IncidentResponse:
        pending: list[str] = []
        if incident.plan is not None:
            pending = [
                action.id
                for action in incident.plan.actions
                if action.requires_approval
                and (
                    (decision := incident.decision_for(action.id)) is None
                    or not decision.approved
                )
            ]
        return cls(
            id=incident.id,
            title=incident.title,
            description=incident.description,
            severity=incident.severity,
            status=incident.status,
            services=incident.services,
            created_at=incident.created_at,
            updated_at=incident.updated_at,
            plan=incident.plan,
            pending_approvals=pending,
        )


class InvestigationResponse(_Model):
    """The full audit trail of one investigation."""

    run_id: str
    incident_id: str
    status: IncidentStatus
    started_at: datetime
    finished_at: datetime | None
    anomalies: list[Anomaly]
    hypotheses: list[Hypothesis]
    findings: list[InfrastructureFinding]
    runbooks: list[RunbookMatch]
    plan: ActionPlan | None
    traces: list[NodeTrace]
    errors: list[str]
    input_tokens: int
    output_tokens: int

    @classmethod
    def from_state(cls, state: InvestigationState) -> InvestigationResponse:
        return cls(
            run_id=state.run_id,
            incident_id=state.incident.id,
            status=state.incident.status,
            started_at=state.started_at,
            finished_at=state.finished_at,
            anomalies=state.anomalies,
            hypotheses=state.hypotheses,
            findings=state.findings,
            runbooks=state.runbooks,
            plan=state.plan,
            traces=state.traces,
            errors=state.errors,
            input_tokens=state.usage.input_tokens,
            output_tokens=state.usage.output_tokens,
        )


class ErrorResponse(_Model):
    detail: str
    kind: str
