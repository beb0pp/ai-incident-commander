"""Domain invariants.

These are the rules the rest of the system is allowed to assume: timestamps
carry a timezone, value objects do not mutate, unknown fields are rejected,
and a re-decided action has exactly one decision on record.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aic.domain.models import (
    ActionPlan,
    ApprovalDecision,
    Incident,
    IncidentStatus,
    ProposedAction,
    RiskLevel,
    ServiceRef,
    Signal,
    SignalKind,
)


class TestDomain:
    def test_naive_timestamps_are_rejected(self) -> None:
        """A signal without a timezone silently misorders an incident timeline."""
        with pytest.raises(ValidationError, match="timezone-aware"):
            Signal(
                kind=SignalKind.LOG,
                service=ServiceRef(name="a"),
                timestamp=datetime(2026, 8, 13, 14, 0),
                message="boom",
            )

    def test_timestamps_are_normalized_to_utc(self) -> None:
        from datetime import timedelta, timezone

        signal = Signal(
            kind=SignalKind.LOG,
            service=ServiceRef(name="a"),
            timestamp=datetime(2026, 8, 13, 16, 0, tzinfo=timezone(timedelta(hours=2))),
            message="boom",
        )
        assert signal.timestamp == datetime(2026, 8, 13, 14, 0, tzinfo=UTC)

    def test_value_objects_are_immutable(self) -> None:
        service = ServiceRef(name="checkout-api")
        with pytest.raises(ValidationError):
            service.name = "something-else"  # type: ignore[misc]

    def test_unknown_fields_are_rejected(self) -> None:
        """extra='forbid' is what makes the schemas usable as strict output formats."""
        with pytest.raises(ValidationError):
            ServiceRef(name="a", typo=True)  # type: ignore[call-arg]

    def test_risk_levels_are_ordered(self) -> None:
        ordered = (RiskLevel.READ_ONLY, RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH)
        ranks = [r.rank for r in ordered]
        assert ranks == sorted(ranks)
        assert len(set(ranks)) == 4

    def test_confidence_is_bounded(self) -> None:
        from aic.domain.models import Hypothesis

        with pytest.raises(ValidationError):
            Hypothesis(title="t", reasoning="r", confidence=1.5)

    def test_re_deciding_an_action_replaces_the_previous_decision(self) -> None:
        """An operator must be able to reverse themselves without ambiguity."""
        incident = Incident(title="t")
        incident.record_decision(ApprovalDecision(action_id="a1", approved=True, decided_by="x"))
        incident.record_decision(ApprovalDecision(action_id="a1", approved=False, decided_by="y"))

        assert len(incident.approvals) == 1
        decision = incident.decision_for("a1")
        assert decision is not None and decision.approved is False

    def test_transition_bumps_updated_at(self) -> None:
        incident = Incident(title="t")
        before = incident.updated_at
        incident.transition_to(IncidentStatus.INVESTIGATING)
        assert incident.updated_at >= before
        assert incident.status is IncidentStatus.INVESTIGATING

    def test_plan_lookup_by_action_id(self) -> None:
        action = ProposedAction(title="t", description="d")
        plan = ActionPlan(summary="s", actions=[action])
        assert plan.action(action.id) is action
        assert plan.action("nope") is None
