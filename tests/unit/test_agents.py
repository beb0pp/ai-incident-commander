"""Agent behaviour, driven by a scripted model.

Each test asserts on the domain objects an agent produces — not on prompt text —
so the tests stay meaningful when the prompts are tuned.
"""

from __future__ import annotations

import pytest

from aic.agents.action import ActionAgent, ActionDraft, ActionPlanDraft
from aic.agents.diagnostic import DiagnosticAgent, DiagnosticOutput, EvidenceDraft, HypothesisDraft
from aic.agents.infrastructure import FindingDraft, InfrastructureAgent, InfrastructureOutput
from aic.agents.monitoring import AnomalyDraft, MonitoringAgent, MonitoringOutput
from aic.agents.runbook import RunbookAgent, RunbookSelection, SelectedRunbook
from aic.config import Settings
from aic.domain.errors import LLMError
from aic.domain.models import (
    Hypothesis,
    IncidentStatus,
    InfrastructureFinding,
    RiskLevel,
    RunbookMatch,
    Severity,
    SignalKind,
)
from aic.guardrails.policy import ActionPolicy
from aic.llm.fake import ScriptedLLMClient, call_every_tool
from aic.orchestration.state import InvestigationState
from aic.rag.embeddings import HashingEmbedding
from aic.rag.indexer import index_directory
from aic.rag.retriever import RunbookRetriever
from aic.rag.store import InMemoryVectorStore
from aic.tools.inspection import build_inspection_tools
from aic.tools.registry import ToolRegistry
from aic.tools.simulated import SimulatedInfrastructure


class TestMonitoringAgent:
    async def test_builds_anomalies_from_cited_signals(
        self, scripted_llm: ScriptedLLMClient, state: InvestigationState
    ) -> None:
        ids = [s.id for s in state.signals[:3]]
        scripted_llm.register(
            MonitoringOutput,
            lambda _: MonitoringOutput(
                anomalies=[
                    AnomalyDraft(
                        service_name="checkout-api",
                        summary="error burst",
                        kind=SignalKind.LOG,
                        severity_hint=Severity.SEV2,
                        score=0.9,
                        signal_ids=ids,
                    )
                ]
            ),
        )

        await MonitoringAgent(scripted_llm).run(state)

        assert len(state.anomalies) == 1
        anomaly = state.anomalies[0]
        assert anomaly.signal_ids == ids
        # Timestamps are derived from the signals, never taken from the model.
        cited = [s for s in state.signals if s.id in ids]
        assert anomaly.first_seen == min(s.timestamp for s in cited)
        assert anomaly.last_seen == max(s.timestamp for s in cited)

    async def test_drops_an_anomaly_that_cites_no_real_signal(
        self, scripted_llm: ScriptedLLMClient, state: InvestigationState
    ) -> None:
        """The hallucination guard: unsourced output must not seed a hypothesis."""
        scripted_llm.register(
            MonitoringOutput,
            lambda _: MonitoringOutput(
                anomalies=[
                    AnomalyDraft(
                        service_name="ghost",
                        summary="invented",
                        kind=SignalKind.LOG,
                        severity_hint=Severity.SEV1,
                        score=0.99,
                        signal_ids=["not-a-real-id"],
                    )
                ]
            ),
        )

        await MonitoringAgent(scripted_llm).run(state)
        assert state.anomalies == []

    async def test_no_signals_means_no_model_call(
        self, scripted_llm: ScriptedLLMClient, state: InvestigationState
    ) -> None:
        state.signals = []
        await MonitoringAgent(scripted_llm).run(state)
        assert scripted_llm.calls == []

    async def test_anomalies_are_ordered_by_score(
        self, scripted_llm: ScriptedLLMClient, state: InvestigationState
    ) -> None:
        ids = [s.id for s in state.signals[:1]]
        scripted_llm.register(
            MonitoringOutput,
            lambda _: MonitoringOutput(
                anomalies=[
                    AnomalyDraft(
                        service_name="a",
                        summary="low",
                        kind=SignalKind.LOG,
                        severity_hint=Severity.SEV3,
                        score=0.2,
                        signal_ids=ids,
                    ),
                    AnomalyDraft(
                        service_name="b",
                        summary="high",
                        kind=SignalKind.LOG,
                        severity_hint=Severity.SEV1,
                        score=0.9,
                        signal_ids=ids,
                    ),
                ]
            ),
        )

        await MonitoringAgent(scripted_llm).run(state)
        assert [a.summary for a in state.anomalies] == ["high", "low"]

    async def test_missing_handler_surfaces_as_an_llm_error(
        self, scripted_llm: ScriptedLLMClient, state: InvestigationState
    ) -> None:
        with pytest.raises(LLMError):
            await MonitoringAgent(scripted_llm).run(state)


class TestDiagnosticAgent:
    async def test_hypotheses_are_ranked_and_capped(
        self, scripted_llm: ScriptedLLMClient, state: InvestigationState
    ) -> None:
        scripted_llm.register(
            DiagnosticOutput,
            lambda _: DiagnosticOutput(
                hypotheses=[
                    HypothesisDraft(
                        title=f"h{i}",
                        reasoning="because",
                        confidence=i / 10,
                        suspected_services=["checkout-api"],
                        evidence=[
                            EvidenceDraft(source="anomaly", reference="x", detail="d")
                        ],
                    )
                    for i in range(8)
                ]
            ),
        )

        await DiagnosticAgent(scripted_llm).run(state)

        assert len(state.hypotheses) == 5
        assert state.hypotheses[0].confidence > state.hypotheses[-1].confidence

    async def test_evidence_with_an_unknown_source_is_discarded(
        self, scripted_llm: ScriptedLLMClient, state: InvestigationState
    ) -> None:
        scripted_llm.register(
            DiagnosticOutput,
            lambda _: DiagnosticOutput(
                hypotheses=[
                    HypothesisDraft(
                        title="h",
                        reasoning="r",
                        confidence=0.5,
                        suspected_services=[],
                        evidence=[
                            EvidenceDraft(source="vibes", reference="x", detail="d"),
                            EvidenceDraft(source="signal", reference="y", detail="d"),
                        ],
                    )
                ]
            ),
        )

        await DiagnosticAgent(scripted_llm).run(state)
        assert [e.source for e in state.hypotheses[0].evidence] == ["signal"]


class TestInfrastructureAgent:
    @pytest.fixture
    def registry(self) -> ToolRegistry:
        return ToolRegistry(build_inspection_tools(SimulatedInfrastructure()))

    async def test_calls_tools_and_records_findings(
        self, scripted_llm: ScriptedLLMClient, state: InvestigationState, registry: ToolRegistry
    ) -> None:
        scripted_llm.tool_planner = lambda tools, _: [("list_active_alarms", {})]
        scripted_llm.register(
            InfrastructureOutput,
            lambda _: InfrastructureOutput(
                findings=[
                    FindingDraft(
                        tool="list_active_alarms",
                        resource="checkout-api",
                        summary="5xx alarm firing",
                        healthy=False,
                    )
                ]
            ),
        )

        await InfrastructureAgent(scripted_llm, registry).run(state)

        assert len(state.findings) == 1
        assert state.unhealthy_findings()

    async def test_no_tool_calls_means_no_findings_and_no_extraction(
        self, scripted_llm: ScriptedLLMClient, state: InvestigationState, registry: ToolRegistry
    ) -> None:
        scripted_llm.tool_planner = lambda tools, _: []
        await InfrastructureAgent(scripted_llm, registry).run(state)

        assert state.findings == []
        assert [c.kind for c in scripted_llm.calls] == ["tool_loop"]

    async def test_exhausting_the_tool_budget_is_reported(
        self, scripted_llm: ScriptedLLMClient, state: InvestigationState, registry: ToolRegistry
    ) -> None:
        """A truncated investigation must say so rather than look complete."""
        scripted_llm.tool_planner = lambda tools, _: [("list_active_alarms", {})] * 50
        scripted_llm.register(
            InfrastructureOutput, lambda _: InfrastructureOutput(findings=[])
        )

        await InfrastructureAgent(scripted_llm, registry).run(state)
        assert any("tool budget" in error for error in state.errors)

    async def test_every_tool_is_reachable_through_the_loop(
        self, scripted_llm: ScriptedLLMClient, state: InvestigationState
    ) -> None:
        """A smoke test that no bundled tool 400s on its own empty-arg call."""
        registry = ToolRegistry(
            [
                t
                for t in build_inspection_tools(SimulatedInfrastructure())
                if t.input_model.__name__ == "NoArgs"
            ]
        )
        scripted_llm.tool_planner = call_every_tool
        scripted_llm.register(
            InfrastructureOutput, lambda _: InfrastructureOutput(findings=[])
        )

        await InfrastructureAgent(scripted_llm, registry).run(state)
        assert len(scripted_llm.calls) == 2  # tool_loop + extraction


class TestRunbookAgent:
    @pytest.fixture
    def retriever(self) -> RunbookRetriever:
        store = InMemoryVectorStore(HashingEmbedding(dimensions=512))
        index_directory(store, Settings().runbook_directory)
        return RunbookRetriever(store)

    async def test_keeps_only_the_selected_candidates(
        self,
        scripted_llm: ScriptedLLMClient,
        state: InvestigationState,
        retriever: RunbookRetriever,
    ) -> None:
        captured: dict[str, list[str]] = {}

        def select(prompt: str) -> RunbookSelection:
            import re

            ids = re.findall(r"^### (\S+) — ", prompt, re.MULTILINE)
            captured["ids"] = ids
            return RunbookSelection(
                selected=[
                    SelectedRunbook(document_id=ids[0], applies_because="matches the failure mode")
                ]
            )

        scripted_llm.register(RunbookSelection, select)
        await RunbookAgent(scripted_llm, retriever).run(state)

        assert len(state.runbooks) == 1
        assert state.runbooks[0].document_id == captured["ids"][0]

    async def test_an_empty_selection_is_respected(
        self,
        scripted_llm: ScriptedLLMClient,
        state: InvestigationState,
        retriever: RunbookRetriever,
    ) -> None:
        """No runbook beats a wrong runbook."""
        scripted_llm.register(RunbookSelection, lambda _: RunbookSelection(selected=[]))
        await RunbookAgent(scripted_llm, retriever).run(state)
        assert state.runbooks == []

    async def test_no_candidates_means_no_model_call(
        self, scripted_llm: ScriptedLLMClient, state: InvestigationState
    ) -> None:
        empty = RunbookRetriever(InMemoryVectorStore(HashingEmbedding()))
        await RunbookAgent(scripted_llm, empty).run(state)
        assert scripted_llm.calls == []


class TestActionAgent:
    async def test_guardrails_rewrite_the_models_plan(
        self, scripted_llm: ScriptedLLMClient, state: InvestigationState
    ) -> None:
        scripted_llm.register(
            ActionPlanDraft,
            lambda _: ActionPlanDraft(
                summary="roll it back",
                actions=[
                    ActionDraft(
                        title="rollback",
                        description="revert the deploy",
                        command="aws ecs update-service --cluster prod --service checkout-api",
                        target_service="checkout-api",
                        declared_risk=RiskLevel.LOW,
                        rationale="smallest reversible change",
                    )
                ],
            ),
        )

        await ActionAgent(scripted_llm, ActionPolicy()).run(state)

        assert state.plan is not None
        action = state.plan.actions[0]
        assert action.risk is RiskLevel.MEDIUM  # reclassified upward
        assert action.requires_approval is True
        assert state.incident.status is IncidentStatus.AWAITING_APPROVAL

    async def test_denylisted_action_is_removed_and_reported(
        self, scripted_llm: ScriptedLLMClient, state: InvestigationState
    ) -> None:
        scripted_llm.register(
            ActionPlanDraft,
            lambda _: ActionPlanDraft(
                summary="drastic",
                actions=[
                    ActionDraft(
                        title="reset the data",
                        description="start clean",
                        command="psql -c 'DROP DATABASE orders'",
                        declared_risk=RiskLevel.MEDIUM,
                        rationale="it would certainly stop the errors",
                    )
                ],
            ),
        )

        await ActionAgent(scripted_llm, ActionPolicy()).run(state)

        assert state.plan is not None
        assert state.plan.actions == []
        assert any("guardrails" in error for error in state.errors)

    async def test_evidence_is_labelled_with_where_it_actually_came_from(
        self, scripted_llm: ScriptedLLMClient, state: InvestigationState
    ) -> None:
        """Provenance has to be right, or the audit trail sends readers astray."""
        state.hypotheses = [
            Hypothesis(title="pool exhaustion", reasoning="r", confidence=0.72)
        ]
        state.findings = [
            InfrastructureFinding(
                tool="describe_rds_instance",
                resource="prod-aurora-orders",
                summary="connections at 199/200",
                healthy=False,
            )
        ]
        state.runbooks = [
            RunbookMatch(
                document_id="database-connection-exhaustion#4",
                title="Database Connection Pool Exhaustion — Rollback",
                excerpt="…",
                score=0.21,
            )
        ]
        cited = [
            state.hypotheses[0].id,
            state.findings[0].id,
            state.runbooks[0].document_id,
        ]

        scripted_llm.register(
            ActionPlanDraft,
            lambda _: ActionPlanDraft(
                summary="roll it back",
                actions=[
                    ActionDraft(
                        title="rollback",
                        description="revert the deploy",
                        command="aws ecs update-service --cluster prod --service checkout-api",
                        declared_risk=RiskLevel.MEDIUM,
                        rationale="smallest reversible change",
                        evidence_refs=cited,
                    )
                ],
            ),
        )

        await ActionAgent(scripted_llm, ActionPolicy()).run(state)

        assert state.plan is not None
        evidence = state.plan.actions[0].evidence
        assert [e.source for e in evidence] == ["hypothesis", "tool", "runbook"]
        assert [e.reference for e in evidence] == cited
        # The detail is readable on its own, not a bare uuid.
        assert "pool exhaustion" in evidence[0].detail
        assert "0.72" in evidence[0].detail
        assert "prod-aurora-orders" in evidence[1].detail
        assert "Rollback" in evidence[2].detail

    async def test_an_invented_reference_is_dropped_and_reported(
        self, scripted_llm: ScriptedLLMClient, state: InvestigationState
    ) -> None:
        """Recording a false citation would be worse than recording none."""
        state.hypotheses = [Hypothesis(title="real one", reasoning="r", confidence=0.6)]

        scripted_llm.register(
            ActionPlanDraft,
            lambda _: ActionPlanDraft(
                summary="s",
                actions=[
                    ActionDraft(
                        title="rollback",
                        description="d",
                        command="aws ecs update-service --cluster prod --service checkout",
                        declared_risk=RiskLevel.MEDIUM,
                        rationale="r",
                        evidence_refs=[state.hypotheses[0].id, "totally-made-up-id"],
                    )
                ],
            ),
        )

        await ActionAgent(scripted_llm, ActionPolicy()).run(state)

        assert state.plan is not None
        evidence = state.plan.actions[0].evidence
        assert [e.reference for e in evidence] == [state.hypotheses[0].id]
        assert any("totally-made-up-id" in error for error in state.errors)

    async def test_an_all_read_only_plan_needs_no_approval(
        self, scripted_llm: ScriptedLLMClient, state: InvestigationState
    ) -> None:
        scripted_llm.register(
            ActionPlanDraft,
            lambda _: ActionPlanDraft(
                summary="just look",
                actions=[
                    ActionDraft(
                        title="check connections",
                        description="read the connection count",
                        command="aws rds describe-db-instances --db-instance-identifier prod",
                        declared_risk=RiskLevel.HIGH,
                        rationale="establish the facts",
                    )
                ],
            ),
        )

        await ActionAgent(scripted_llm, ActionPolicy()).run(state)

        assert state.plan is not None
        # Declared HIGH, reclassified DOWN to read_only — the policy cuts both ways.
        assert state.plan.actions[0].risk is RiskLevel.READ_ONLY
        assert state.incident.status is IncidentStatus.INVESTIGATING
