"""Anthropic adapter for the :mod:`aic.llm.base` port.

Two deliberate choices worth calling out (see ``docs/adr/0003``):

* **Structured outputs over prose parsing.** Agents get a validated Pydantic
  instance or an :class:`~aic.domain.errors.LLMError`. There is no "best effort
  regex the JSON out of the answer" path anywhere in this project.
* **A hand-written tool loop.** The SDK ships a tool runner, but the loop is the
  part of an agent platform worth showing: it is where retries, per-call
  auditing, and the guardrail hooks live. Writing it also keeps us off a beta
  surface.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, TypeVar

import anthropic
from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

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
from aic.llm.schema import to_strict_json_schema

T = TypeVar("T", bound=BaseModel)

#: Transient failures worth retrying. Everything else fails fast.
_RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
)

_retry_transient = retry(
    retry=retry_if_exception_type(_RETRYABLE),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    reraise=True,
)


class AnthropicLLMClient:
    """Implements :class:`aic.llm.base.LLMClient` against the Claude API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "claude-opus-5",
        effort: str = "high",
        max_tokens: int = 16_000,
        timeout_seconds: float = 120.0,
        client: AsyncAnthropic | None = None,
    ) -> None:
        # A bare AsyncAnthropic() also resolves ANTHROPIC_AUTH_TOKEN and an
        # `ant auth login` profile, so an unset api_key is not an error here.
        self._client = client or AsyncAnthropic(api_key=api_key, timeout=timeout_seconds)
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens

    # -- structured output ------------------------------------------------

    async def structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: type[T],
        max_tokens: int | None = None,
    ) -> StructuredResult[T]:
        response = await self._create(
            system=system,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens or self._max_tokens,
            output_config={
                "effort": self._effort,
                "format": {
                    "type": "json_schema",
                    "schema": to_strict_json_schema(schema),
                },
            },
        )
        self._reject_refusal(response)
        payload = _first_text(response)
        if not payload:
            raise LLMError(f"model returned no text for {schema.__name__}")
        try:
            value = schema.model_validate_json(payload)
        except ValidationError as exc:
            raise LLMError(f"model output failed {schema.__name__} validation: {exc}") from exc
        return StructuredResult(value=value, usage=_usage(response))

    # -- tool calling -----------------------------------------------------

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
        tool_defs = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        calls: list[ToolCall] = []
        outcomes: list[ToolOutcome] = []
        usage = Usage()
        text = ""

        for _ in range(max_iterations):
            response = await self._create(
                system=system,
                messages=messages,
                max_tokens=max_tokens or self._max_tokens,
                tools=tool_defs,
                output_config={"effort": self._effort},
            )
            self._reject_refusal(response)
            usage = usage + _usage(response)
            text = _first_text(response) or text
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "pause_turn":
                # A server-side tool hit its iteration cap; re-send to resume.
                continue

            pending = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            if not pending:
                return ToolLoopResult(
                    text=text, calls=calls, outcomes=outcomes, usage=usage, stopped_early=False
                )

            results: list[dict[str, Any]] = []
            for block in pending:
                call = ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {}))
                calls.append(call)
                outcome = await execute(call)
                outcomes.append(outcome)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": outcome.call_id,
                        "content": outcome.content,
                        "is_error": outcome.is_error,
                    }
                )
            # All results for one assistant turn go back in a single user message.
            messages.append({"role": "user", "content": results})

        return ToolLoopResult(
            text=text, calls=calls, outcomes=outcomes, usage=usage, stopped_early=True
        )

    # -- plumbing ---------------------------------------------------------

    @_retry_transient
    async def _create(self, **kwargs: Any) -> Any:
        try:
            return await self._client.messages.create(model=self._model, **kwargs)
        except anthropic.APIStatusError as exc:
            raise LLMError(f"model provider returned {exc.status_code}: {exc.message}") from exc

    @staticmethod
    def _reject_refusal(response: Any) -> None:
        """Safety classifiers return HTTP 200 with an empty body — check first."""
        if getattr(response, "stop_reason", None) != "refusal":
            return
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) or "unspecified"
        raise LLMError(f"model declined the request (category={category})")


def _first_text(response: Any) -> str:
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return str(block.text)
    return ""


def _usage(response: Any) -> Usage:
    raw = getattr(response, "usage", None)
    if raw is None:
        return Usage()
    return Usage(
        input_tokens=getattr(raw, "input_tokens", 0) or 0,
        output_tokens=getattr(raw, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
    )


def json_dumps(value: Any) -> str:
    """Deterministic serialization for tool results (stable keys keep caches warm)."""
    return json.dumps(value, sort_keys=True, default=str)
