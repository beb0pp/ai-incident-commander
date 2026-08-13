"""Domain-level exceptions.

Every failure the platform surfaces to an operator is modelled here, so the API
layer never has to guess an HTTP status from a bare ``Exception``.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for every error the platform raises on purpose."""


class LLMError(DomainError):
    """The model provider failed, timed out, or returned an unusable response."""


class ToolExecutionError(DomainError):
    """A tool was called correctly but failed while executing."""

    def __init__(self, tool_name: str, message: str) -> None:
        super().__init__(f"tool {tool_name!r} failed: {message}")
        self.tool_name = tool_name
        self.message = message


class AgentError(DomainError):
    """An agent could not produce a usable result after exhausting its retries."""

    def __init__(self, agent_name: str, message: str) -> None:
        super().__init__(f"agent {agent_name!r} failed: {message}")
        self.agent_name = agent_name
        self.message = message


class GuardrailViolationError(DomainError):
    """A proposed action was rejected by policy and must never be executed."""


class ApprovalRequiredError(DomainError):
    """Execution was attempted on an action that carries no human approval."""


class NotFoundError(DomainError):
    """A requested resource does not exist."""
