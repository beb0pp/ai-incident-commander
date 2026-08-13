"""Tool abstraction shared by every agent.

A tool is a typed, side-effect-declared capability. The typing matters: the
model is handed a JSON Schema derived from ``input_model``, and every call is
re-validated against that same model before any code runs, so a hallucinated
argument becomes a clean ``is_error`` tool result instead of a ``TypeError``
somewhere deep in an integration.

The ``risk`` attribute is what lets the registry enforce, structurally, that an
investigation can only ever read (see :mod:`aic.guardrails`).
"""

from __future__ import annotations

import abc
from typing import Any, ClassVar

from pydantic import BaseModel

from aic.domain.models import RiskLevel
from aic.llm.base import ToolSpec
from aic.llm.schema import to_strict_json_schema


class Tool(abc.ABC):
    """Base class for anything an agent may call."""

    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[BaseModel]]
    risk: ClassVar[RiskLevel] = RiskLevel.READ_ONLY

    def spec(self) -> ToolSpec:
        """The definition handed to the model."""
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=to_strict_json_schema(self.input_model),
        )

    @abc.abstractmethod
    async def run(self, args: Any) -> dict[str, Any]:
        """Execute the tool. ``args`` is a validated ``input_model`` instance."""


class NoArgs(BaseModel):
    """Input model for tools that take no parameters."""

    model_config = {"extra": "forbid"}
