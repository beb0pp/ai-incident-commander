"""Shared agent scaffolding.

An agent is a narrow, testable unit: it reads :class:`InvestigationState`,
performs exactly one kind of reasoning, and writes its output back. It never
calls another agent — composition is the orchestrator's job. That separation is
what keeps each agent unit-testable against a scripted model, and what lets the
graph parallelise the two that do not depend on each other.
"""

from __future__ import annotations

import abc
from typing import ClassVar, TypeVar

import structlog
from pydantic import BaseModel

from aic.llm.base import LLMClient, StructuredResult
from aic.orchestration.state import InvestigationState

T = TypeVar("T", bound=BaseModel)

log = structlog.get_logger(__name__)


class Agent(abc.ABC):
    """Base class for every agent in the platform."""

    name: ClassVar[str]
    #: A node whose failure should degrade rather than abort the investigation.
    optional: ClassVar[bool] = False
    #: What this agent needs to have run before it can do its job.
    depends_on: ClassVar[tuple[str, ...]] = ()

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm
        self.log = structlog.get_logger(f"aic.agents.{self.name}")

    @abc.abstractmethod
    async def run(self, state: InvestigationState) -> None:
        """Read from and write to ``state``. Raise to signal failure."""

    # An agent deliberately knows nothing about the graph engine. The pipeline
    # reads ``name``/``depends_on``/``optional`` and builds the nodes, which
    # keeps the dependency pointing one way: orchestration -> agents.

    # -- helpers ----------------------------------------------------------

    async def _ask(
        self,
        state: InvestigationState,
        *,
        system: str,
        prompt: str,
        schema: type[T],
    ) -> T:
        """One structured model call, with usage folded into the run's totals."""
        result: StructuredResult[T] = await self._llm.structured(
            system=system, prompt=prompt, schema=schema
        )
        state.usage.add(
            result.usage.input_tokens,
            result.usage.output_tokens,
            result.usage.cache_read_tokens,
        )
        return result.value
