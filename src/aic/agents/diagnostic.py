"""Diagnostic Agent — correlate anomalies into ranked root-cause hypotheses."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from aic.agents.base import Agent
from aic.agents.prompts import DIAGNOSTIC_SYSTEM
from aic.domain.models import Confidence, Evidence, Hypothesis, ServiceRef
from aic.orchestration.state import InvestigationState

MAX_HYPOTHESES = 5


class EvidenceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(description="One of: signal, anomaly, tool, runbook.")
    reference: str = Field(description="The id of the cited item.")
    detail: str = Field(description="What this item shows.")


class HypothesisDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="The candidate root cause, in one line.")
    reasoning: str = Field(description="Why the evidence points here.")
    confidence: Confidence
    suspected_services: list[str] = Field(description="Service names implicated.")
    evidence: list[EvidenceDraft]


class DiagnosticOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypotheses: list[HypothesisDraft]


class DiagnosticAgent(Agent):
    """Produces competing explanations rather than one confident answer."""

    name: ClassVar[str] = "diagnostic"
    depends_on: ClassVar[tuple[str, ...]] = ("monitoring",)

    async def run(self, state: InvestigationState) -> None:
        draft = await self._ask(
            state,
            system=DIAGNOSTIC_SYSTEM,
            prompt=_build_prompt(state),
            schema=DiagnosticOutput,
        )

        known_services = {s.service.name: s.service for s in state.signals}
        hypotheses = [
            Hypothesis(
                title=item.title,
                reasoning=item.reasoning,
                confidence=item.confidence,
                suspected_services=[
                    known_services.get(name) or ServiceRef(name=name)
                    for name in item.suspected_services
                ],
                evidence=_coerce_evidence(item.evidence),
            )
            for item in draft.hypotheses
        ]
        hypotheses.sort(key=lambda h: h.confidence, reverse=True)
        state.hypotheses = hypotheses[:MAX_HYPOTHESES]
        self.log.info("diagnostic.done", count=len(state.hypotheses), run_id=state.run_id)


def _coerce_evidence(drafts: list[EvidenceDraft]) -> list[Evidence]:
    """Keep only evidence whose source is one the domain model recognises."""
    valid = {"signal", "anomaly", "tool", "runbook"}
    return [
        Evidence(source=d.source, detail=d.detail, reference=d.reference)  # type: ignore[arg-type]
        for d in drafts
        if d.source in valid
    ]


def _build_prompt(state: InvestigationState) -> str:
    lines = [
        f"Incident: {state.incident.title}",
        f"Description: {state.incident.description or '(none provided)'}",
        "",
        "## Triaged anomalies",
    ]
    if state.anomalies:
        for anomaly in state.anomalies:
            lines.append(
                f"- [{anomaly.id}] {anomaly.service} ({anomaly.kind}, "
                f"score={anomaly.score:.2f}, hint={anomaly.severity_hint}) "
                f"{anomaly.first_seen.isoformat()} → {anomaly.last_seen.isoformat()}: "
                f"{anomaly.summary}"
            )
    else:
        lines.append("(none — the Monitoring Agent found nothing worth escalating)")

    lines += ["", "## Supporting signals"]
    cited = {sid for a in state.anomalies for sid in a.signal_ids}
    supporting = [s for s in state.signals if s.id in cited] or state.signals[:40]
    for signal in sorted(supporting, key=lambda s: s.timestamp)[:60]:
        value = f" value={signal.value}" if signal.value is not None else ""
        lines.append(
            f"- [{signal.id}] {signal.timestamp.isoformat()} {signal.service} "
            f"{signal.kind}: {signal.message}{value}"
        )

    lines += [
        "",
        "Produce ranked root-cause hypotheses. Distinguish cause from symptom, and "
        "cite the anomaly or signal ids that support each one.",
    ]
    return "\n".join(lines)
