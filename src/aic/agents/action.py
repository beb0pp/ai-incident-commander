"""Action Agent — consolidate everything into a plan, then hand it to guardrails.

The plan the model produces is *not* the plan the platform publishes. Between
the two sits :class:`~aic.guardrails.policy.ActionPolicy`, which re-derives each
action's risk from its command text and forces the approval flag. The model's
self-reported risk is kept only as an audit trail of what it claimed.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from aic.agents.base import Agent
from aic.agents.prompts import ACTION_SYSTEM
from aic.domain.models import (
    ActionPlan,
    Evidence,
    IncidentStatus,
    ProposedAction,
    RiskLevel,
    ServiceRef,
)
from aic.guardrails.policy import ActionPolicy
from aic.llm.base import LLMClient
from aic.orchestration.state import InvestigationState


class ActionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="Imperative one-liner, e.g. 'Roll back checkout-api'.")
    description: str = Field(description="What this does and what it should achieve.")
    command: str | None = Field(
        default=None, description="Exact command or API call, when one exists."
    )
    target_service: str | None = Field(default=None, description="Service this acts on.")
    declared_risk: RiskLevel = Field(description="Your assessment of the blast radius.")
    rationale: str = Field(description="Why this action follows from the evidence.")
    rollback: str | None = Field(default=None, description="How to undo it.")
    evidence_refs: list[str] = Field(
        default_factory=list, description="Ids of hypotheses, findings, or runbooks cited."
    )


class ActionPlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="What is happening and what the plan does about it.")
    actions: list[ActionDraft]


class ActionAgent(Agent):
    """Produces the remediation plan and applies the safety policy to it."""

    name: ClassVar[str] = "action"
    depends_on: ClassVar[tuple[str, ...]] = ("infrastructure", "runbook")

    def __init__(self, llm: LLMClient, policy: ActionPolicy) -> None:
        super().__init__(llm)
        self._policy = policy

    async def run(self, state: InvestigationState) -> None:
        draft = await self._ask(
            state,
            system=ACTION_SYSTEM,
            prompt=_build_prompt(state),
            schema=ActionPlanDraft,
        )

        services = {s.name: s for s in state.incident.services}
        for signal in state.signals:
            services.setdefault(signal.service.name, signal.service)

        index = _evidence_index(state)
        actions: list[ProposedAction] = []
        unresolved: list[str] = []

        for item in draft.actions:
            evidence = [index[ref] for ref in item.evidence_refs if ref in index]
            unresolved.extend(ref for ref in item.evidence_refs if ref not in index)
            actions.append(
                ProposedAction(
                    title=item.title,
                    description=item.description,
                    command=item.command,
                    target=(
                        services.get(item.target_service) or ServiceRef(name=item.target_service)
                        if item.target_service
                        else None
                    ),
                    risk=item.declared_risk,
                    rationale=item.rationale,
                    rollback=item.rollback,
                    evidence=evidence,
                )
            )

        if unresolved:
            # An action citing something this investigation never produced is a
            # broken audit trail. Drop the citation rather than record a false
            # one, and surface it — silently discarding it would hide the very
            # failure mode the evidence model exists to catch.
            state.errors.append(
                "action: dropped evidence references that match nothing in this "
                f"investigation: {sorted(set(unresolved))}"
            )
            self.log.warning(
                "action.unresolved_evidence",
                references=sorted(set(unresolved)),
                run_id=state.run_id,
            )

        raw_plan = ActionPlan(summary=draft.summary, actions=actions)

        plan, decisions = self._policy.apply(raw_plan)
        state.plan = plan
        state.incident.plan = plan

        for decision in decisions:
            if decision.reason.startswith("denied:"):
                state.errors.append(
                    f"action {decision.action_id} removed by guardrails ({decision.reason})"
                )

        needs_human = any(a.requires_approval for a in plan.actions)
        state.incident.transition_to(
            IncidentStatus.AWAITING_APPROVAL if needs_human else IncidentStatus.INVESTIGATING
        )
        self.log.info(
            "action.done",
            proposed=len(draft.actions),
            kept=len(plan.actions),
            awaiting_approval=needs_human,
            run_id=state.run_id,
        )


def _evidence_index(state: InvestigationState) -> dict[str, Evidence]:
    """Map every id the model could have cited back onto what produced it.

    The keys are exactly the ids rendered into the prompt, so a reference that
    misses this index is one the model invented. Each entry carries a readable
    ``detail`` too — "the hypothesis about pool exhaustion, at 0.72 confidence"
    is worth more to a responder than a bare uuid.
    """
    index: dict[str, Evidence] = {}

    for anomaly in state.anomalies:
        index[anomaly.id] = Evidence(
            source="anomaly", reference=anomaly.id, detail=anomaly.summary
        )
    for hypothesis in state.hypotheses:
        index[hypothesis.id] = Evidence(
            source="hypothesis",
            reference=hypothesis.id,
            detail=f"{hypothesis.title} (confidence {hypothesis.confidence:.2f})",
        )
    for finding in state.findings:
        index[finding.id] = Evidence(
            source="tool",
            reference=finding.id,
            detail=f"{finding.tool} on {finding.resource}: {finding.summary}",
        )
    for runbook in state.runbooks:
        index[runbook.document_id] = Evidence(
            source="runbook", reference=runbook.document_id, detail=runbook.title
        )

    return index


def _build_prompt(state: InvestigationState) -> str:
    lines = [
        f"Incident: {state.incident.title} (severity {state.incident.severity})",
        f"Description: {state.incident.description or '(none provided)'}",
        "",
        "## Hypotheses",
    ]
    lines += [
        f"- [{h.id}] ({h.confidence:.2f}) {h.title} — {h.reasoning}" for h in state.hypotheses
    ] or ["(none)"]

    lines += ["", "## Infrastructure findings"]
    lines += [
        f"- [{f.id}] {f.tool} on {f.resource}: {f.summary} "
        f"({'healthy' if f.healthy else 'UNHEALTHY'})"
        for f in state.findings
    ] or ["(none — infrastructure inspection produced no findings)"]

    lines += ["", "## Applicable runbooks"]
    for runbook in state.runbooks:
        lines.append(f"\n### {runbook.document_id} — {runbook.title}")
        for step in runbook.steps[:12]:
            lines.append(f"  - {step}")
    if not state.runbooks:
        lines.append("(none matched this incident)")

    lines += [
        "",
        "Produce the remediation plan. Diagnosis and reversible mitigation first; "
        "anything destructive last and only if the evidence demands it. Cite the "
        "hypothesis, finding, or runbook id behind each action.",
    ]
    return "\n".join(lines)
