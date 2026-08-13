"""Safety policy for anything the platform proposes to do.

The design rule this module encodes: **the model's own risk assessment is never
trusted.** The Action Agent reports what it thinks an action's blast radius is,
and this module recomputes it from the action's actual command text. The
recomputed value wins. A prompt injection that talks the model into labelling
``DROP DATABASE`` as ``read_only`` still lands in front of a human.

Three layers, in order:

1. **Denylist** — a small set of operations that are never legitimate incident
   mitigation. Rejected outright, not queued for approval.
2. **Risk reclassification** — pattern-based, from the command text.
3. **Approval gate** — anything above the configured auto-approve ceiling
   requires a recorded human decision before it can execute. ``HIGH`` always
   requires one, whatever the configuration says.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

from aic.domain.errors import ApprovalRequiredError, GuardrailViolationError
from aic.domain.models import ActionPlan, ApprovalDecision, ProposedAction, RiskLevel
from aic.infrastructure.observability import metrics

log = structlog.get_logger(__name__)

#: Never legitimate incident mitigation. Proposing one is a bug or an attack.
DENYLIST: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("recursive filesystem deletion", re.compile(r"\brm\s+-[a-z]*[rR][a-z]*f\b")),
    ("database or table drop", re.compile(r"\bdrop\s+(database|table|schema)\b", re.I)),
    ("unbounded delete", re.compile(r"\bdelete\s+from\s+\w+\s*(;|$)", re.I)),
    (
        "disabling audit logging",
        re.compile(r"\b(delete-trail|stop-logging|disable-logging)\b", re.I),
    ),
    (
        "credential exfiltration",
        re.compile(r"\b(get-secret-value|describe-db-snapshot-attributes)\b.*\bcurl\b", re.I),
    ),
    ("IAM privilege escalation", re.compile(r"\b(attach-user-policy|put-user-policy)\b", re.I)),
)

#: Command patterns mapped to their true blast radius, most dangerous first.
RISK_PATTERNS: tuple[tuple[RiskLevel, re.Pattern[str]], ...] = (
    (
        RiskLevel.HIGH,
        re.compile(
            r"\b(delete-|terminate-|reboot-db|failover|restore-db|modify-db-cluster|"
            r"truncate|flushall|flushdb|purge-queue)\b",
            re.I,
        ),
    ),
    (
        RiskLevel.MEDIUM,
        re.compile(
            r"\b(update-service|deploy|rollback|restart|scale|set-desired-capacity|"
            r"modify-db-instance|put-scaling-policy|redrive)\b",
            re.I,
        ),
    ),
    (
        RiskLevel.LOW,
        re.compile(
            r"\b(set-alarm-state|put-metric-alarm|tag-resource|annotate|create-ticket)\b", re.I
        ),
    ),
    (
        RiskLevel.READ_ONLY,
        re.compile(r"\b(describe|list|get|show|head|select)\b", re.I),
    ),
)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Why an action ended up with the risk and approval flag that it did."""

    action_id: str
    declared_risk: RiskLevel
    effective_risk: RiskLevel
    requires_approval: bool
    reason: str


class ActionPolicy:
    """Applies the guardrails to a proposed plan."""

    def __init__(self, auto_approve_max_risk: RiskLevel = RiskLevel.READ_ONLY) -> None:
        if auto_approve_max_risk.rank >= RiskLevel.HIGH.rank:
            # A configuration that auto-approves destructive actions defeats the
            # entire Human-in-the-Loop design, so it is not configurable.
            raise GuardrailViolationError(
                "auto_approve_max_risk may not be 'high'; high-risk actions always "
                "require an explicit human decision"
            )
        self._ceiling = auto_approve_max_risk

    @property
    def auto_approve_ceiling(self) -> RiskLevel:
        return self._ceiling

    # -- classification ---------------------------------------------------

    @staticmethod
    def check_denylist(action: ProposedAction) -> str | None:
        """Return the reason an action is forbidden outright, or ``None``."""
        haystack = " ".join(filter(None, [action.command, action.title, action.description]))
        for reason, pattern in DENYLIST:
            if pattern.search(haystack):
                return reason
        return None

    @staticmethod
    def classify(action: ProposedAction) -> RiskLevel:
        """Recompute an action's risk from its command text.

        Absent a command there is nothing to pattern-match, so the action is
        treated as ``HIGH``: unknown blast radius is the dangerous kind.
        """
        if not action.command:
            return RiskLevel.HIGH
        for level, pattern in RISK_PATTERNS:
            if pattern.search(action.command):
                return level
        return RiskLevel.HIGH

    def requires_approval(self, risk: RiskLevel) -> bool:
        return risk.rank > self._ceiling.rank

    # -- application ------------------------------------------------------

    def apply(self, plan: ActionPlan) -> tuple[ActionPlan, list[PolicyDecision]]:
        """Return a policy-corrected plan plus an audit record of every change.

        Denied actions are dropped from the plan and recorded in the decisions,
        so an operator can see what was filtered and why.
        """
        kept: list[ProposedAction] = []
        decisions: list[PolicyDecision] = []

        for action in plan.actions:
            denied = self.check_denylist(action)
            if denied is not None:
                log.warning("guardrail.denied", action=action.title, reason=denied)
                metrics.guardrail_events_total.labels(kind="denied").inc()
                decisions.append(
                    PolicyDecision(
                        action_id=action.id,
                        declared_risk=action.risk,
                        effective_risk=RiskLevel.HIGH,
                        requires_approval=True,
                        reason=f"denied: {denied}",
                    )
                )
                continue

            effective = self.classify(action)
            needs_approval = self.requires_approval(effective)
            if effective is not action.risk:
                metrics.guardrail_events_total.labels(kind="reclassified").inc()
                log.info(
                    "guardrail.reclassified",
                    action=action.title,
                    declared=str(action.risk),
                    effective=str(effective),
                )
            reason = (
                "risk reclassified from declared value"
                if effective is not action.risk
                else "risk confirmed"
            )
            decisions.append(
                PolicyDecision(
                    action_id=action.id,
                    declared_risk=action.risk,
                    effective_risk=effective,
                    requires_approval=needs_approval,
                    reason=reason,
                )
            )
            kept.append(
                action.model_copy(
                    update={"risk": effective, "requires_approval": needs_approval}
                )
            )

        return plan.model_copy(update={"actions": kept}), decisions

    # -- execution gate ---------------------------------------------------

    def assert_executable(
        self, action: ProposedAction, decision: ApprovalDecision | None
    ) -> None:
        """Raise unless ``action`` may run right now.

        This is the last gate before anything touches infrastructure. It is
        intentionally separate from :meth:`apply`, so re-checking is cheap and
        the caller cannot skip it by holding a stale plan.
        """
        if self.check_denylist(action) is not None:
            raise GuardrailViolationError(f"action {action.id!r} is denied by policy")

        effective = self.classify(action)
        if not self.requires_approval(effective):
            return

        if decision is None:
            raise ApprovalRequiredError(
                f"action {action.id!r} has risk {effective} and no approval on record"
            )
        if not decision.approved:
            raise GuardrailViolationError(
                f"action {action.id!r} was rejected by {decision.decided_by}"
            )
