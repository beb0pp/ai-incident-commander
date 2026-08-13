"""The guardrail layer is the safety-critical part of this project.

These tests encode the properties the README claims: the model's risk assessment
is never trusted, denylisted operations are removed rather than gated, and no
configuration can auto-approve a destructive action.
"""

from __future__ import annotations

import pytest

from aic.domain.errors import ApprovalRequiredError, GuardrailViolationError
from aic.domain.models import ActionPlan, ApprovalDecision, ProposedAction, RiskLevel
from aic.guardrails.policy import ActionPolicy


def action(**kwargs: object) -> ProposedAction:
    defaults: dict[str, object] = {
        "title": "do a thing",
        "description": "a thing gets done",
        "risk": RiskLevel.READ_ONLY,
    }
    return ProposedAction(**{**defaults, **kwargs})  # type: ignore[arg-type]


class TestRiskReclassification:
    def test_understated_risk_is_corrected_upward(self, policy: ActionPolicy) -> None:
        """A rollback declared 'low' is really a MEDIUM mutation of a live service."""
        declared_low = action(
            command="aws ecs update-service --cluster prod --service checkout-api",
            risk=RiskLevel.LOW,
        )
        assert policy.classify(declared_low) is RiskLevel.MEDIUM

    def test_destructive_command_declared_read_only_is_still_high(
        self, policy: ActionPolicy
    ) -> None:
        """The scenario a prompt injection would aim for."""
        lying = action(
            command="aws rds delete-db-instance --db-instance-identifier prod",
            risk=RiskLevel.READ_ONLY,
        )
        assert policy.classify(lying) is RiskLevel.HIGH
        assert policy.requires_approval(policy.classify(lying))

    def test_read_only_command_stays_read_only(self, policy: ActionPolicy) -> None:
        reading = action(command="aws rds describe-db-instances --db-instance-identifier prod")
        assert policy.classify(reading) is RiskLevel.READ_ONLY
        assert not policy.requires_approval(RiskLevel.READ_ONLY)

    def test_action_without_a_command_is_treated_as_high(self, policy: ActionPolicy) -> None:
        """Unknown blast radius is the dangerous kind, so it gets the dangerous label."""
        assert policy.classify(action(command=None)) is RiskLevel.HIGH


class TestDenylist:
    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf /var/lib/postgresql",
            "psql -c 'DROP DATABASE orders'",
            "psql -c 'DELETE FROM orders;'",
            "aws cloudtrail stop-logging --name prod-trail",
            "aws iam attach-user-policy --user-name svc "
            "--policy-arn arn:aws:iam::aws:policy/AdministratorAccess",
        ],
    )
    def test_denied_commands_are_rejected(self, policy: ActionPolicy, command: str) -> None:
        assert policy.check_denylist(action(command=command)) is not None

    def test_denied_actions_are_removed_from_the_plan_entirely(
        self, policy: ActionPolicy
    ) -> None:
        """Removed, not queued for approval — a human should never see the option."""
        plan = ActionPlan(
            summary="mixed plan",
            actions=[
                action(title="safe", command="aws ecs describe-services --cluster prod"),
                action(title="catastrophic", command="psql -c 'DROP TABLE orders'"),
            ],
        )
        cleaned, decisions = policy.apply(plan)

        assert [a.title for a in cleaned.actions] == ["safe"]
        assert any(d.reason.startswith("denied:") for d in decisions)

    def test_denylist_also_inspects_title_and_description(self, policy: ActionPolicy) -> None:
        """A command field is not the only place a dangerous instruction can hide."""
        sneaky = action(
            title="cleanup",
            description="run rm -rf /data to reclaim space",
            command=None,
        )
        assert policy.check_denylist(sneaky) is not None


class TestApprovalGate:
    def test_high_risk_cannot_be_auto_approved_by_configuration(self) -> None:
        with pytest.raises(GuardrailViolationError):
            ActionPolicy(auto_approve_max_risk=RiskLevel.HIGH)

    def test_raising_the_ceiling_auto_approves_below_it(self) -> None:
        lenient = ActionPolicy(auto_approve_max_risk=RiskLevel.MEDIUM)
        rollback = action(command="aws ecs update-service --cluster prod --service checkout")
        assert not lenient.requires_approval(lenient.classify(rollback))

    def test_execution_without_a_decision_is_refused(self, policy: ActionPolicy) -> None:
        gated = action(command="aws ecs update-service --cluster prod --service checkout")
        with pytest.raises(ApprovalRequiredError):
            policy.assert_executable(gated, None)

    def test_execution_after_rejection_is_refused(self, policy: ActionPolicy) -> None:
        gated = action(command="aws ecs update-service --cluster prod --service checkout")
        rejection = ApprovalDecision(
            action_id=gated.id, approved=False, decided_by="sre-oncall"
        )
        with pytest.raises(GuardrailViolationError):
            policy.assert_executable(gated, rejection)

    def test_execution_after_approval_is_allowed(self, policy: ActionPolicy) -> None:
        gated = action(command="aws ecs update-service --cluster prod --service checkout")
        approval = ApprovalDecision(action_id=gated.id, approved=True, decided_by="sre-oncall")
        policy.assert_executable(gated, approval)  # does not raise

    def test_read_only_needs_no_decision(self, policy: ActionPolicy) -> None:
        safe = action(command="aws ecs list-services --cluster prod")
        policy.assert_executable(safe, None)  # does not raise

    def test_approval_cannot_launder_a_denylisted_action(self, policy: ActionPolicy) -> None:
        """Even a signed-off DROP DATABASE stays refused."""
        forbidden = action(command="psql -c 'DROP DATABASE orders'")
        approval = ApprovalDecision(
            action_id=forbidden.id, approved=True, decided_by="someone-with-a-keyboard"
        )
        with pytest.raises(GuardrailViolationError):
            policy.assert_executable(forbidden, approval)


class TestPolicyApplication:
    def test_apply_rewrites_risk_and_approval_flags(self, policy: ActionPolicy) -> None:
        plan = ActionPlan(
            summary="s",
            actions=[
                action(
                    command="aws ecs update-service --cluster prod --service checkout",
                    risk=RiskLevel.LOW,
                    requires_approval=False,
                )
            ],
        )
        cleaned, decisions = policy.apply(plan)

        assert cleaned.actions[0].risk is RiskLevel.MEDIUM
        assert cleaned.actions[0].requires_approval is True
        assert decisions[0].declared_risk is RiskLevel.LOW
        assert decisions[0].effective_risk is RiskLevel.MEDIUM
