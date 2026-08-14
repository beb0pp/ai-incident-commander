"""Tool registry: the risk gate, argument validation, and failure containment."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ConfigDict

from aic.domain.errors import GuardrailViolationError, ToolExecutionError
from aic.domain.models import RiskLevel
from aic.llm.base import ToolCall
from aic.tools.base import NoArgs, Tool
from aic.tools.inspection import build_inspection_tools
from aic.tools.registry import ToolRegistry
from aic.tools.simulated import SimulatedInfrastructure


class EchoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class EchoTool(Tool):
    name = "echo"
    description = "Echo a value back."
    input_model = EchoArgs

    async def run(self, args: EchoArgs) -> dict[str, object]:
        return {"echoed": args.value}


class ExplodingTool(Tool):
    name = "explode"
    description = "Always fails."
    input_model = NoArgs

    async def run(self, args: NoArgs) -> dict[str, object]:
        raise ToolExecutionError("explode", "the integration is down")


class RestartTool(Tool):
    name = "restart_service"
    description = "Restarts a service."
    input_model = NoArgs
    risk = RiskLevel.HIGH

    async def run(self, args: NoArgs) -> dict[str, object]:
        return {"restarted": True}


class TestRiskGate:
    def test_read_only_registry_refuses_a_mutating_tool(self) -> None:
        """The structural guarantee behind 'an investigation can only read'."""
        with pytest.raises(GuardrailViolationError, match="exceeds the registry limit"):
            ToolRegistry([RestartTool()], max_risk=RiskLevel.READ_ONLY)

    def test_a_permissive_registry_accepts_it(self) -> None:
        registry = ToolRegistry([RestartTool()], max_risk=RiskLevel.HIGH)
        assert "restart_service" in registry

    def test_every_bundled_infrastructure_tool_is_read_only(self) -> None:
        for tool in build_inspection_tools(SimulatedInfrastructure()):
            assert tool.risk is RiskLevel.READ_ONLY, tool.name

    def test_duplicate_registration_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            ToolRegistry([EchoTool(), EchoTool()])


class TestDispatch:
    async def test_successful_call_returns_serialized_payload(self) -> None:
        registry = ToolRegistry([EchoTool()])
        outcome = await registry.execute(ToolCall(id="1", name="echo", arguments={"value": "hi"}))

        assert not outcome.is_error
        assert json.loads(outcome.content) == {"echoed": "hi"}

    async def test_unknown_tool_becomes_an_error_result_not_an_exception(self) -> None:
        """The model must be able to see and correct its own mistake."""
        registry = ToolRegistry([EchoTool()])
        outcome = await registry.execute(ToolCall(id="1", name="nope", arguments={}))

        assert outcome.is_error
        assert "echo" in outcome.content  # the available tools are listed

    async def test_invalid_arguments_become_an_error_result(self) -> None:
        registry = ToolRegistry([EchoTool()])
        outcome = await registry.execute(ToolCall(id="1", name="echo", arguments={"wrong": 1}))

        assert outcome.is_error
        assert "Invalid arguments" in outcome.content

    async def test_tool_failure_is_contained(self) -> None:
        """One broken integration must not abort an investigation."""
        registry = ToolRegistry([ExplodingTool()])
        outcome = await registry.execute(ToolCall(id="1", name="explode", arguments={}))

        assert outcome.is_error
        assert "the integration is down" in outcome.content

    async def test_call_id_is_always_echoed_back(self) -> None:
        """A mismatched tool_use_id breaks the conversation, so check every path."""
        registry = ToolRegistry([EchoTool(), ExplodingTool()])
        for call in (
            ToolCall(id="a", name="echo", arguments={"value": "x"}),
            ToolCall(id="b", name="echo", arguments={}),
            ToolCall(id="c", name="explode", arguments={}),
            ToolCall(id="d", name="ghost", arguments={}),
        ):
            outcome = await registry.execute(call)
            assert outcome.call_id == call.id


class TestSpecs:
    def test_specs_are_stably_ordered(self) -> None:
        """Stable tool order keeps the cached prompt prefix byte-identical."""
        registry = ToolRegistry(build_inspection_tools(SimulatedInfrastructure()))
        assert [s.name for s in registry.specs()] == sorted(registry.names)

    def test_spec_schema_is_structured_output_compatible(self) -> None:
        registry = ToolRegistry([EchoTool()])
        schema = registry.specs()[0].input_schema
        assert schema["additionalProperties"] is False


class TestAwsTools:
    async def test_describes_a_known_service(self) -> None:
        registry = ToolRegistry(build_inspection_tools(SimulatedInfrastructure()))
        outcome = await registry.execute(
            ToolCall(id="1", name="describe_ecs_service", arguments={"service": "checkout-api"})
        )
        payload = json.loads(outcome.content)
        assert payload["runningCount"] < payload["desiredCount"]

    async def test_unknown_resource_lists_what_exists(self) -> None:
        """An error the model can act on beats an error it can only report."""
        registry = ToolRegistry(build_inspection_tools(SimulatedInfrastructure()))
        outcome = await registry.execute(
            ToolCall(id="1", name="describe_ecs_service", arguments={"service": "ghost-api"})
        )
        assert outcome.is_error
        assert "checkout-api" in outcome.content

    async def test_only_firing_alarms_are_returned(self) -> None:
        registry = ToolRegistry(build_inspection_tools(SimulatedInfrastructure()))
        outcome = await registry.execute(
            ToolCall(id="1", name="list_active_alarms", arguments={})
        )
        payload = json.loads(outcome.content)
        assert payload["count"] == len(payload["alarms"])
        assert all(alarm["state"] == "ALARM" for alarm in payload["alarms"])
