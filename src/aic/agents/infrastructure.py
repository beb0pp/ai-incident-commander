"""Infrastructure Agent — verify hypotheses by actually calling read-only tools.

This is the only agent that runs a tool-calling loop. Its registry is built with
``max_risk=READ_ONLY``, so the "it can only look, never touch" property is
structural: the registry refuses to even hold a mutating tool, which means no
prompt can talk this agent into one.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from aic.agents.base import Agent
from aic.agents.prompts import INFRASTRUCTURE_SYSTEM
from aic.domain.models import InfrastructureFinding
from aic.llm.base import LLMClient, ToolLoopResult
from aic.orchestration.state import InvestigationState
from aic.tools.registry import ToolRegistry

MAX_TOOL_ITERATIONS = 8
MAX_TRANSCRIPT_CHARS = 12_000


class FindingDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(description="Which tool established this fact.")
    resource: str = Field(description="The resource inspected, e.g. 'prod-aurora-orders'.")
    summary: str = Field(description="What the tool output shows, in one line.")
    healthy: bool = Field(description="False if this resource looks like part of the problem.")


class InfrastructureOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[FindingDraft]


class InfrastructureAgent(Agent):
    """Confirms or refutes hypotheses against the live(ish) environment."""

    name: ClassVar[str] = "infrastructure"
    depends_on: ClassVar[tuple[str, ...]] = ("diagnostic",)
    #: Tool integrations are the flakiest part of a real deployment. An
    #: investigation without infrastructure findings is degraded, not dead.
    optional: ClassVar[bool] = True

    def __init__(self, llm: LLMClient, registry: ToolRegistry) -> None:
        super().__init__(llm)
        self._registry = registry

    async def run(self, state: InvestigationState) -> None:
        loop = await self._llm.tool_loop(
            system=INFRASTRUCTURE_SYSTEM,
            prompt=_build_prompt(state, self._registry),
            tools=self._registry.specs(),
            execute=self._registry.execute,
            max_iterations=MAX_TOOL_ITERATIONS,
        )
        state.usage.add(
            loop.usage.input_tokens, loop.usage.output_tokens, loop.usage.cache_read_tokens
        )

        if loop.stopped_early:
            state.errors.append(
                f"infrastructure: tool budget of {MAX_TOOL_ITERATIONS} iterations exhausted"
            )

        if not loop.calls:
            self.log.info("infrastructure.no_tool_calls", run_id=state.run_id)
            return

        draft = await self._ask(
            state,
            system=INFRASTRUCTURE_SYSTEM,
            prompt=_summarize_prompt(loop),
            schema=InfrastructureOutput,
        )

        state.findings = [
            InfrastructureFinding(
                tool=item.tool,
                resource=item.resource,
                summary=item.summary,
                healthy=item.healthy,
                raw={"transcript_call_count": len(loop.calls)},
            )
            for item in draft.findings
        ]
        self.log.info(
            "infrastructure.done",
            calls=len(loop.calls),
            findings=len(state.findings),
            unhealthy=len(state.unhealthy_findings()),
            run_id=state.run_id,
        )


def _build_prompt(state: InvestigationState, registry: ToolRegistry) -> str:
    lines = [
        f"Incident: {state.incident.title}",
        "",
        "## Hypotheses to test",
    ]
    if state.hypotheses:
        for hypothesis in state.hypotheses:
            lines.append(
                f"- [{hypothesis.id}] ({hypothesis.confidence:.2f}) {hypothesis.title} "
                f"— {hypothesis.reasoning}"
            )
    else:
        lines.append("(none yet — establish the current state of the affected services)")

    anomaly_lines = [f"- {a.service}: {a.summary}" for a in state.anomalies] or ["(none)"]
    lines += [
        "",
        "## Anomalies",
        *anomaly_lines,
        "",
        f"Available read-only tools: {', '.join(registry.names)}.",
        "Call the tools that would confirm or refute the hypotheses above, then "
        "report what you found — including resources that turned out to be healthy.",
    ]
    return "\n".join(lines)


def _summarize_prompt(loop: ToolLoopResult) -> str:
    """Render the tool transcript for the structured extraction pass."""
    lines = ["Tool transcript from this investigation:", ""]
    for call, outcome in zip(loop.calls, loop.outcomes, strict=False):
        status = "ERROR" if outcome.is_error else "OK"
        lines.append(f"### {call.name}({call.arguments}) -> {status}")
        lines.append(outcome.content)
        lines.append("")

    transcript = "\n".join(lines)
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        transcript = transcript[:MAX_TRANSCRIPT_CHARS] + "\n… (transcript truncated)"

    return (
        f"{transcript}\n"
        "Convert this transcript into discrete findings. One finding per resource "
        "inspected. Mark a resource unhealthy only if its own output shows a problem."
    )
