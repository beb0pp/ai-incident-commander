"""Runbook Agent — retrieve operational procedures, then judge whether they apply.

Retrieval and judgement are deliberately separate steps. Lexical retrieval will
happily return a procedure that shares vocabulary with the incident but treats a
different failure mode, and a responder under pressure will follow it. So the
retriever proposes and the model disposes.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from aic.agents.base import Agent
from aic.agents.prompts import RUNBOOK_SYSTEM
from aic.domain.models import RunbookMatch
from aic.llm.base import LLMClient
from aic.orchestration.state import InvestigationState
from aic.rag.retriever import RunbookRetriever


class SelectedRunbook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(description="Id of a retrieved candidate, copied exactly.")
    applies_because: str = Field(description="Why this procedure fits this incident.")


class RunbookSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected: list[SelectedRunbook] = Field(
        description="Only the candidates that genuinely apply. May be empty."
    )


class RunbookAgent(Agent):
    """Narrows retrieved candidates down to the procedures that actually apply."""

    name: ClassVar[str] = "runbook"
    depends_on: ClassVar[tuple[str, ...]] = ("diagnostic",)
    #: Missing documentation should not stop an investigation.
    optional: ClassVar[bool] = True

    def __init__(self, llm: LLMClient, retriever: RunbookRetriever) -> None:
        super().__init__(llm)
        self._retriever = retriever

    async def run(self, state: InvestigationState) -> None:
        candidates = self._retriever.retrieve(_build_query(state))
        if not candidates:
            self.log.info("runbook.no_candidates", run_id=state.run_id)
            return

        selection = await self._ask(
            state,
            system=RUNBOOK_SYSTEM,
            prompt=_build_prompt(state, candidates),
            schema=RunbookSelection,
        )

        keep = {s.document_id: s.applies_because for s in selection.selected}
        state.runbooks = [c for c in candidates if c.document_id in keep]
        self.log.info(
            "runbook.done",
            retrieved=len(candidates),
            kept=len(state.runbooks),
            run_id=state.run_id,
        )


def _build_query(state: InvestigationState) -> str:
    """Build the retrieval query from the incident plus what has been learned."""
    parts = [state.incident.title, state.incident.description]
    parts += [h.title for h in state.hypotheses[:3]]
    parts += [a.summary for a in state.anomalies[:5]]
    parts += [str(s) for s in state.incident.services]
    return " ".join(p for p in parts if p)


def _build_prompt(state: InvestigationState, candidates: list[RunbookMatch]) -> str:
    lines = [
        f"Incident: {state.incident.title}",
        "",
        "## Current leading hypotheses",
    ]
    hypotheses = [
        f"- ({h.confidence:.2f}) {h.title}" for h in state.hypotheses[:3]
    ] or ["(none established yet)"]
    lines += hypotheses

    lines += ["", "## Retrieved candidate procedures"]
    for candidate in candidates:
        lines.append(
            f"\n### {candidate.document_id} — {candidate.title} "
            f"(similarity {candidate.score:.3f})"
        )
        lines.append(candidate.excerpt)

    lines += [
        "",
        "Return only the candidates that genuinely address this incident's failure "
        "mode. Copy document ids exactly. Returning an empty list is the right "
        "answer when nothing fits.",
    ]
    return "\n".join(lines)
