"""The LLM port.

Everything above this line in the dependency graph (agents, orchestration, API)
depends only on these protocols — never on a vendor SDK. That is what makes the
agents testable without a network and what would make swapping providers a
single-file change.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class Usage:
    """Token accounting for a single model interaction."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
        )


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool definition as the model sees it."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class StructuredResult[T: BaseModel]:
    value: T
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True, slots=True)
class ToolLoopResult:
    """What a tool-calling conversation produced, plus the audit trail."""

    text: str
    calls: list[ToolCall] = field(default_factory=list)
    outcomes: list[ToolOutcome] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stopped_early: bool = False


ToolExecutor = Callable[[ToolCall], Awaitable[ToolOutcome]]


class LLMClient(Protocol):
    """The two model interactions this platform needs. Nothing more."""

    async def structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: type[T],
        max_tokens: int | None = None,
    ) -> StructuredResult[T]:
        """Return a validated instance of ``schema``, never free-form text."""
        ...

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
        """Run the model/tool conversation until the model stops calling tools."""
        ...
