"""Application service — the use cases, independent of HTTP.

The API layer translates JSON to these calls and back. Nothing here knows what
a request or a status code is, which is what lets the same use cases be driven
from a CLI, a queue consumer, or a test.
"""

from __future__ import annotations

import structlog

from aic.domain.errors import NotFoundError
from aic.domain.models import (
    ApprovalDecision,
    Incident,
    IncidentStatus,
    ProposedAction,
    ServiceRef,
    Severity,
    Signal,
)
from aic.guardrails.policy import ActionPolicy
from aic.infrastructure.db.repository import IncidentRepository
from aic.infrastructure.observability import metrics, run_id_var
from aic.orchestration.checkpoint import CheckpointStore
from aic.orchestration.pipeline import InvestigationRunner
from aic.orchestration.state import InvestigationState

log = structlog.get_logger(__name__)


class IncidentService:
    """Create incidents, investigate them, and gate the resulting actions."""

    def __init__(
        self,
        *,
        repository: IncidentRepository,
        runner: InvestigationRunner,
        policy: ActionPolicy,
        checkpoints: CheckpointStore,
    ) -> None:
        self._repository = repository
        self._runner = runner
        self._policy = policy
        self._checkpoints = checkpoints
        #: incident_id -> run_id of the most recent investigation.
        self._latest_run: dict[str, str] = {}

    # -- incidents --------------------------------------------------------

    async def create_incident(
        self,
        *,
        title: str,
        description: str = "",
        severity: Severity = Severity.SEV3,
        services: list[ServiceRef] | None = None,
    ) -> Incident:
        incident = Incident(
            title=title,
            description=description,
            severity=severity,
            services=services or [],
        )
        await self._repository.save(incident)
        log.info("incident.created", incident_id=incident.id, severity=str(severity))
        return incident

    async def get_incident(self, incident_id: str) -> Incident:
        return await self._repository.get(incident_id)

    async def list_incidents(self, *, limit: int = 50) -> list[Incident]:
        return await self._repository.list(limit=limit)

    # -- investigation ----------------------------------------------------

    async def investigate(self, incident_id: str, signals: list[Signal]) -> InvestigationState:
        """Run the full agent pipeline over ``signals`` for an existing incident."""
        incident = await self._repository.get(incident_id)
        state = await self._runner.run(incident, signals)
        run_id_var.set(state.run_id)
        self._latest_run[incident.id] = state.run_id
        await self._repository.save(incident)
        return state

    async def get_investigation(self, run_id: str) -> InvestigationState:
        state = await self._checkpoints.load(run_id)
        if state is None:
            raise NotFoundError(f"investigation {run_id!r} not found")
        return state

    async def latest_investigation(self, incident_id: str) -> InvestigationState:
        run_id = self._latest_run.get(incident_id)
        if run_id is None:
            raise NotFoundError(f"no investigation has been run for incident {incident_id!r}")
        return await self.get_investigation(run_id)

    # -- human in the loop ------------------------------------------------

    async def decide(
        self,
        *,
        incident_id: str,
        action_id: str,
        approved: bool,
        decided_by: str,
        comment: str | None = None,
    ) -> tuple[Incident, ProposedAction]:
        """Record a human decision on one proposed action."""
        incident = await self._repository.get(incident_id)
        if incident.plan is None:
            raise NotFoundError(f"incident {incident_id!r} has no action plan yet")

        action = incident.plan.action(action_id)
        if action is None:
            raise NotFoundError(f"action {action_id!r} is not part of this incident's plan")

        incident.record_decision(
            ApprovalDecision(
                action_id=action_id,
                approved=approved,
                decided_by=decided_by,
                comment=comment,
            )
        )

        if approved and not self._has_pending_approvals(incident):
            incident.transition_to(IncidentStatus.MITIGATING)

        await self._repository.save(incident)
        metrics.approvals_total.labels(outcome="approved" if approved else "rejected").inc()
        log.info(
            "approval.recorded",
            incident_id=incident_id,
            action_id=action_id,
            approved=approved,
            decided_by=decided_by,
        )
        return incident, action

    def assert_executable(self, incident: Incident, action: ProposedAction) -> None:
        """The final gate. Call this immediately before any execution attempt.

        Nothing in this repository executes actions — deliberately, see the README
        — but the gate exists so an executor added later cannot forget to ask.
        """
        self._policy.assert_executable(action, incident.decision_for(action.id))

    @staticmethod
    def _has_pending_approvals(incident: Incident) -> bool:
        if incident.plan is None:
            return False
        return any(
            action.requires_approval
            and (
                (decision := incident.decision_for(action.id)) is None or not decision.approved
            )
            for action in incident.plan.actions
        )
