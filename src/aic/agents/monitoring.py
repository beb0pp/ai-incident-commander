"""Monitoring Agent — normalize telemetry and triage it into anomalies."""

from __future__ import annotations

from collections import defaultdict
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from aic.agents.base import Agent
from aic.agents.prompts import MONITORING_SYSTEM
from aic.domain.models import Anomaly, Confidence, ServiceRef, Severity, Signal, SignalKind
from aic.orchestration.state import InvestigationState

MAX_SIGNALS_IN_PROMPT = 120


class AnomalyDraft(BaseModel):
    """What the model is asked to produce for each anomaly it identifies."""

    model_config = ConfigDict(extra="forbid")

    service_name: str = Field(description="Service the anomaly belongs to.")
    summary: str = Field(description="One sentence a responder can act on.")
    kind: SignalKind
    severity_hint: Severity
    score: Confidence = Field(description="Confidence this is a real problem, 0-1.")
    signal_ids: list[str] = Field(description="Ids of the signals this anomaly covers.")


class MonitoringOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anomalies: list[AnomalyDraft]


class MonitoringAgent(Agent):
    """Turns a pile of raw signals into a short list of things worth looking at."""

    name: ClassVar[str] = "monitoring"

    async def run(self, state: InvestigationState) -> None:
        if not state.signals:
            self.log.info("monitoring.no_signals", run_id=state.run_id)
            return

        draft = await self._ask(
            state,
            system=MONITORING_SYSTEM,
            prompt=_build_prompt(state),
            schema=MonitoringOutput,
        )

        by_id = {s.id: s for s in state.signals}
        services = {s.service.name: s.service for s in state.signals}

        anomalies: list[Anomaly] = []
        for item in draft.anomalies:
            covered = [by_id[sid] for sid in item.signal_ids if sid in by_id]
            if not covered:
                # An anomaly citing no real signal is a hallucination; drop it
                # rather than let it seed a hypothesis downstream.
                self.log.warning("monitoring.dropped_unsourced", summary=item.summary)
                continue
            anomalies.append(
                Anomaly(
                    service=services.get(item.service_name)
                    or ServiceRef(name=item.service_name),
                    summary=item.summary,
                    kind=item.kind,
                    # Timestamps come from the signals, never from the model.
                    first_seen=min(s.timestamp for s in covered),
                    last_seen=max(s.timestamp for s in covered),
                    signal_ids=[s.id for s in covered],
                    severity_hint=item.severity_hint,
                    score=item.score,
                )
            )

        anomalies.sort(key=lambda a: a.score, reverse=True)
        state.anomalies = anomalies
        self.log.info("monitoring.done", count=len(anomalies), run_id=state.run_id)


def _build_prompt(state: InvestigationState) -> str:
    """Render the incident and its telemetry, densest-first under a size budget."""
    grouped: dict[str, list[Signal]] = defaultdict(list)
    for signal in state.signals:
        grouped[signal.service.name].append(signal)

    lines = [
        f"Incident: {state.incident.title}",
        f"Reported severity: {state.incident.severity}",
        f"Description: {state.incident.description or '(none provided)'}",
        "",
        f"Signals ({len(state.signals)} total across {len(grouped)} services):",
    ]

    budget = MAX_SIGNALS_IN_PROMPT
    for service in sorted(grouped, key=lambda s: -len(grouped[s])):
        signals = sorted(grouped[service], key=lambda s: s.timestamp)
        share = max(1, budget // max(1, len(grouped)))
        shown = signals[:share]
        lines.append(f"\n## {service} ({len(signals)} signals)")
        for signal in shown:
            value = f" value={signal.value}" if signal.value is not None else ""
            labels = f" {signal.labels}" if signal.labels else ""
            lines.append(
                f"- [{signal.id}] {signal.timestamp.isoformat()} "
                f"{signal.kind}: {signal.message}{value}{labels}"
            )
        if len(signals) > len(shown):
            lines.append(f"- … and {len(signals) - len(shown)} more of the same shape")
        budget -= len(shown)

    return "\n".join(lines)
