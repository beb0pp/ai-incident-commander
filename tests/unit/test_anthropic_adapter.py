"""The Anthropic adapter, driven by a stub SDK client.

The tool loop is the piece ADR 0003 claims value for, so it gets tested rather
than asserted about. No network: a stub stands in for ``AsyncAnthropic`` and
returns the same block shapes the real SDK does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from aic.domain.errors import LLMError
from aic.llm.anthropic_client import AnthropicLLMClient
from aic.llm.base import ToolCall, ToolOutcome, ToolSpec


class Extracted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    healthy: bool


# -- stub SDK objects, shaped like the real response blocks -------------------


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class Usage:
    input_tokens: int = 10
    output_tokens: int = 5
    cache_read_input_tokens: int = 2


@dataclass
class StubResponse:
    content: list[Any]
    stop_reason: str = "end_turn"
    stop_details: Any = None
    usage: Usage = field(default_factory=Usage)


@dataclass
class StubMessages:
    responses: list[StubResponse]
    requests: list[dict[str, Any]] = field(default_factory=list)

    async def create(self, **kwargs: Any) -> StubResponse:
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError("stub ran out of scripted responses")
        return self.responses.pop(0)


@dataclass
class StubClient:
    messages: StubMessages


def build(responses: list[StubResponse]) -> tuple[AnthropicLLMClient, StubMessages]:
    messages = StubMessages(responses=responses)
    client = AnthropicLLMClient(client=StubClient(messages=messages))  # type: ignore[arg-type]
    return client, messages


WEATHER_TOOL = ToolSpec(
    name="describe_ecs_service",
    description="Describe a service.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)


async def echo_executor(call: ToolCall) -> ToolOutcome:
    return ToolOutcome(call_id=call.id, content=f"result for {call.name}", is_error=False)


def _tool_result_messages(sent: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every user message carrying tool results, in order."""
    return [
        m
        for m in sent
        if m["role"] == "user"
        and isinstance(m["content"], list)
        and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
    ]


def _find_tool_results(sent: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The blocks of the first tool-result message."""
    messages = _tool_result_messages(sent)
    assert messages, "no tool_result message was ever sent back"
    return list(messages[0]["content"])


class TestStructured:
    async def test_validated_instance_is_returned(self) -> None:
        client, _ = build([StubResponse(content=[TextBlock('{"service":"a","healthy":true}')])])

        result = await client.structured(system="s", prompt="p", schema=Extracted)

        assert result.value == Extracted(service="a", healthy=True)
        assert result.usage.input_tokens == 10
        assert result.usage.cache_read_tokens == 2

    async def test_the_request_carries_a_sanitized_schema_and_the_effort(self) -> None:
        client, messages = build(
            [StubResponse(content=[TextBlock('{"service":"a","healthy":true}')])]
        )
        await client.structured(system="s", prompt="p", schema=Extracted)

        output_config = messages.requests[0]["output_config"]
        assert output_config["effort"] == "high"
        assert output_config["format"]["type"] == "json_schema"
        assert output_config["format"]["schema"]["additionalProperties"] is False

    async def test_malformed_json_is_an_llm_error_not_a_crash(self) -> None:
        client, _ = build([StubResponse(content=[TextBlock("not json at all")])])

        with pytest.raises(LLMError, match="failed Extracted validation"):
            await client.structured(system="s", prompt="p", schema=Extracted)

    async def test_schema_violation_is_an_llm_error(self) -> None:
        client, _ = build([StubResponse(content=[TextBlock('{"service":"a"}')])])

        with pytest.raises(LLMError, match="failed Extracted validation"):
            await client.structured(system="s", prompt="p", schema=Extracted)

    async def test_empty_response_is_an_llm_error(self) -> None:
        client, _ = build([StubResponse(content=[])])

        with pytest.raises(LLMError, match="no text"):
            await client.structured(system="s", prompt="p", schema=Extracted)


class TestRefusals:
    async def test_a_refusal_is_raised_not_silently_returned(self) -> None:
        """Safety classifiers return HTTP 200 with an empty body — the trap."""

        @dataclass
        class Details:
            category: str = "cyber"

        client, _ = build(
            [StubResponse(content=[], stop_reason="refusal", stop_details=Details())]
        )

        with pytest.raises(LLMError, match="category=cyber"):
            await client.structured(system="s", prompt="p", schema=Extracted)

    async def test_a_refusal_without_details_is_still_raised(self) -> None:
        client, _ = build([StubResponse(content=[], stop_reason="refusal")])

        with pytest.raises(LLMError, match="unspecified"):
            await client.structured(system="s", prompt="p", schema=Extracted)


class TestToolLoop:
    async def test_executes_tools_and_stops_when_the_model_stops_calling(self) -> None:
        client, _ = build(
            [
                StubResponse(
                    content=[ToolUseBlock(id="t1", name="describe_ecs_service", input={})],
                    stop_reason="tool_use",
                ),
                StubResponse(content=[TextBlock("checkout-api has 8 of 12 tasks running")]),
            ]
        )

        result = await client.tool_loop(
            system="s", prompt="p", tools=[WEATHER_TOOL], execute=echo_executor
        )

        assert [c.name for c in result.calls] == ["describe_ecs_service"]
        assert result.outcomes[0].content == "result for describe_ecs_service"
        assert "8 of 12" in result.text
        assert result.stopped_early is False
        assert result.usage.input_tokens == 20  # both turns accumulated

    async def test_all_results_for_one_turn_go_back_in_a_single_user_message(self) -> None:
        """Splitting them trains the model out of parallel tool calls."""
        client, messages = build(
            [
                StubResponse(
                    content=[
                        ToolUseBlock(id="t1", name="describe_ecs_service", input={}),
                        ToolUseBlock(id="t2", name="describe_ecs_service", input={}),
                    ],
                    stop_reason="tool_use",
                ),
                StubResponse(content=[TextBlock("done")]),
            ]
        )

        result = await client.tool_loop(
            system="s", prompt="p", tools=[WEATHER_TOOL], execute=echo_executor
        )

        assert len(result.calls) == 2
        batches = _tool_result_messages(messages.requests[1]["messages"])
        assert len(batches) == 1, "results were split across turns"
        assert len(batches[0]["content"]) == 2

    async def test_pause_turn_is_resumed_rather_than_treated_as_completion(self) -> None:
        client, _ = build(
            [
                StubResponse(content=[TextBlock("partial")], stop_reason="pause_turn"),
                StubResponse(content=[TextBlock("complete")]),
            ]
        )

        result = await client.tool_loop(
            system="s", prompt="p", tools=[WEATHER_TOOL], execute=echo_executor
        )

        assert result.text == "complete"
        assert result.stopped_early is False

    async def test_exhausting_the_iteration_budget_is_reported(self) -> None:
        """A truncated investigation must not look like a finished one."""
        client, _ = build(
            [
                StubResponse(
                    content=[ToolUseBlock(id=f"t{i}", name="describe_ecs_service", input={})],
                    stop_reason="tool_use",
                )
                for i in range(5)
            ]
        )

        result = await client.tool_loop(
            system="s",
            prompt="p",
            tools=[WEATHER_TOOL],
            execute=echo_executor,
            max_iterations=3,
        )

        assert result.stopped_early is True
        assert len(result.calls) == 3

    async def test_tool_errors_are_forwarded_to_the_model(self) -> None:
        async def failing_executor(call: ToolCall) -> ToolOutcome:
            return ToolOutcome(call_id=call.id, content="resource not found", is_error=True)

        client, messages = build(
            [
                StubResponse(
                    content=[ToolUseBlock(id="t1", name="describe_ecs_service", input={})],
                    stop_reason="tool_use",
                ),
                StubResponse(content=[TextBlock("I will try a different resource")]),
            ]
        )

        result = await client.tool_loop(
            system="s", prompt="p", tools=[WEATHER_TOOL], execute=failing_executor
        )

        assert result.outcomes[0].is_error
        # The stub captures kwargs by reference and the loop mutates the message
        # list in place, so search for the tool_result block rather than index.
        forwarded = _find_tool_results(messages.requests[1]["messages"])
        assert forwarded[0]["is_error"] is True
        assert forwarded[0]["tool_use_id"] == "t1"

    async def test_a_refusal_mid_loop_is_raised(self) -> None:
        client, _ = build([StubResponse(content=[], stop_reason="refusal")])

        with pytest.raises(LLMError):
            await client.tool_loop(
                system="s", prompt="p", tools=[WEATHER_TOOL], execute=echo_executor
            )
