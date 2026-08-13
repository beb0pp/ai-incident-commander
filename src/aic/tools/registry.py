"""Tool dispatch, validation, and failure containment.

Everything a model can trigger goes through here. Three things happen on every
call, in this order:

1. **Risk gate.** A registry built with ``max_risk=READ_ONLY`` physically cannot
   execute a mutating tool. Investigation agents get such a registry, so "the
   model restarted prod" is not a prompt-engineering question in this design.
2. **Argument validation.** Bad arguments become an error *result*, not an
   exception — the model sees the failure and can correct itself.
3. **Failure containment.** A tool that raises returns ``is_error=True`` with the
   message. One broken integration must not abort an investigation.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

import structlog
from pydantic import ValidationError

from aic.domain.errors import GuardrailViolationError
from aic.domain.models import RiskLevel
from aic.infrastructure.observability import metrics
from aic.llm.base import ToolCall, ToolOutcome, ToolSpec
from aic.tools.base import Tool

log = structlog.get_logger(__name__)


class ToolRegistry:
    """Holds the tools available to one agent and executes calls against them."""

    def __init__(self, tools: Iterable[Tool], *, max_risk: RiskLevel = RiskLevel.READ_ONLY) -> None:
        self._tools: dict[str, Tool] = {}
        self._max_risk = max_risk
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        if tool.risk.rank > self._max_risk.rank:
            raise GuardrailViolationError(
                f"tool {tool.name!r} has risk {tool.risk} which exceeds the registry "
                f"limit of {self._max_risk}"
            )
        self._tools[tool.name] = tool

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        """Tool definitions, in a stable order so the prompt prefix stays cacheable."""
        return [self._tools[name].spec() for name in self.names]

    async def execute(self, call: ToolCall) -> ToolOutcome:
        """Run one tool call. Never raises for a tool's own failure."""
        tool = self._tools.get(call.name)
        if tool is None:
            metrics.tool_calls_total.labels(tool=call.name, outcome="unknown_tool").inc()
            return ToolOutcome(
                call_id=call.id,
                content=f"Unknown tool {call.name!r}. Available: {', '.join(self.names)}.",
                is_error=True,
            )

        try:
            args = tool.input_model.model_validate(call.arguments)
        except ValidationError as exc:
            metrics.tool_calls_total.labels(tool=call.name, outcome="invalid_args").inc()
            return ToolOutcome(
                call_id=call.id,
                content=f"Invalid arguments for {call.name!r}: {exc.errors()}",
                is_error=True,
            )

        try:
            payload = await tool.run(args)
        except Exception as exc:
            log.warning("tool.failed", tool=call.name, error=str(exc))
            metrics.tool_calls_total.labels(tool=call.name, outcome="error").inc()
            return ToolOutcome(
                call_id=call.id,
                content=f"Tool {call.name!r} failed: {exc}",
                is_error=True,
            )

        log.debug("tool.ok", tool=call.name, args=call.arguments)
        metrics.tool_calls_total.labels(tool=call.name, outcome="ok").inc()
        return ToolOutcome(
            call_id=call.id,
            content=json.dumps(payload, sort_keys=True, default=str),
            is_error=False,
        )
