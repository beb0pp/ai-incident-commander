"""A scripted in-process LLM.

This exists for two reasons:

* **Tests.** Agent behaviour is asserted against fixed model output, so the test
  suite is fast, offline, and deterministic. No network, no API key, no flake.
* **The demo.** ``AIC_LLM_PROVIDER=fake`` runs the entire pipeline end to end so
  a reviewer can clone the repo and see the system work before deciding whether
  to spend a token on it.

It implements the same :class:`aic.llm.base.LLMClient` protocol as the real
adapter, so nothing upstream can tell the difference.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

from aic.domain.errors import LLMError
from aic.llm.base import (
    StructuredResult,
    ToolCall,
    ToolExecutor,
    ToolLoopResult,
    ToolOutcome,
    ToolSpec,
    Usage,
)

T = TypeVar("T", bound=BaseModel)

#: Given the prompt, produce the object the agent expects back.
StructuredHandler = Callable[[str], BaseModel]

#: Given the available tools and the prompt, decide which tools to call.
ToolPlanner = Callable[[Sequence[ToolSpec], str], list[tuple[str, dict[str, Any]]]]


@dataclass(slots=True)
class Recorded:
    """One captured interaction, for assertions in tests."""

    kind: str
    system: str
    prompt: str
    schema: str | None = None


@dataclass
class ScriptedLLMClient:
    """An :class:`~aic.llm.base.LLMClient` driven by registered handlers."""

    handlers: dict[type[BaseModel], StructuredHandler] = field(default_factory=dict)
    tool_planner: ToolPlanner | None = None
    final_text: str = "Investigation complete."
    calls: list[Recorded] = field(default_factory=list)

    def register(self, schema: type[BaseModel], handler: StructuredHandler) -> None:
        """Bind a response factory to the schema an agent will ask for."""
        self.handlers[schema] = handler

    async def structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: type[T],
        max_tokens: int | None = None,
    ) -> StructuredResult[T]:
        self.calls.append(
            Recorded(kind="structured", system=system, prompt=prompt, schema=schema.__name__)
        )
        handler = self.handlers.get(schema)
        if handler is None:
            raise LLMError(
                f"ScriptedLLMClient has no handler registered for {schema.__name__}. "
                "Register one with .register() before running this agent."
            )
        value = handler(prompt)
        if not isinstance(value, schema):
            raise LLMError(
                f"handler for {schema.__name__} returned {type(value).__name__}"
            )
        return StructuredResult(value=value, usage=Usage(input_tokens=len(prompt) // 4))

    async def tool_loop(
        self,
        *,
        system: str,
        prompt: str,
        tools: Sequence[ToolSpec],
        execute: ToolExecutor,
        max_iterations: int = 8,
        max_tokens: int | None = None,
    ) -> ToolLoopResult:
        self.calls.append(Recorded(kind="tool_loop", system=system, prompt=prompt))
        planned = self.tool_planner(tools, prompt) if self.tool_planner else []
        calls: list[ToolCall] = []
        outcomes: list[ToolOutcome] = []

        for index, (name, arguments) in enumerate(planned[:max_iterations]):
            call = ToolCall(id=f"scripted-{index}", name=name, arguments=arguments)
            calls.append(call)
            outcomes.append(await execute(call))

        return ToolLoopResult(
            text=self.final_text,
            calls=calls,
            outcomes=outcomes,
            usage=Usage(input_tokens=len(prompt) // 4),
            stopped_early=len(planned) > max_iterations,
        )


def call_every_tool(tools: Sequence[ToolSpec], _prompt: str) -> list[tuple[str, dict[str, Any]]]:
    """A tool planner that exercises every read-only tool exactly once."""
    return [(tool.name, {}) for tool in tools]
