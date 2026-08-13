"""Composition root.

Every dependency in the system is constructed here and nowhere else. Agents,
tools, and services take their collaborators as constructor arguments, so this
is the single file you edit to swap Postgres for memory, the real model for the
scripted one, or the simulated environment for a real AWS client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from aic.agents.action import ActionAgent
from aic.agents.diagnostic import DiagnosticAgent
from aic.agents.infrastructure import InfrastructureAgent
from aic.agents.monitoring import MonitoringAgent
from aic.agents.runbook import RunbookAgent
from aic.config import LLMProvider, Settings
from aic.demo import build_demo_llm
from aic.domain.models import RiskLevel
from aic.guardrails.policy import ActionPolicy
from aic.infrastructure.db.repository import (
    IncidentRepository,
    InMemoryIncidentRepository,
    PostgresIncidentRepository,
)
from aic.llm.base import LLMClient
from aic.orchestration.checkpoint import (
    CheckpointStore,
    InMemoryCheckpointStore,
    RedisCheckpointStore,
)
from aic.orchestration.pipeline import Agents, InvestigationRunner, build_graph
from aic.rag.embeddings import HashingEmbedding
from aic.rag.indexer import index_directory
from aic.rag.retriever import RunbookRetriever
from aic.rag.store import InMemoryVectorStore
from aic.service import IncidentService
from aic.tools.aws import build_infrastructure_tools
from aic.tools.environment import SimulatedEnvironment, demo_environment
from aic.tools.registry import ToolRegistry

log = structlog.get_logger(__name__)


@dataclass
class Container:
    """Everything the application needs, already wired together."""

    settings: Settings
    llm: LLMClient
    policy: ActionPolicy
    repository: IncidentRepository
    checkpoints: CheckpointStore
    retriever: RunbookRetriever
    registry: ToolRegistry
    service: IncidentService


def build_llm(settings: Settings) -> LLMClient:
    if settings.llm_provider is LLMProvider.FAKE:
        log.info("llm.scripted", reason="AIC_LLM_PROVIDER=fake")
        return build_demo_llm()

    # Imported lazily so the scripted path never pays for SDK import time.
    from aic.llm.anthropic_client import AnthropicLLMClient

    key = settings.anthropic_api_key
    return AnthropicLLMClient(
        api_key=key.get_secret_value() if key else None,
        model=settings.llm_model,
        effort=settings.llm_effort,
        max_tokens=settings.llm_max_tokens,
        timeout_seconds=settings.llm_timeout_seconds,
    )


def build_retriever(settings: Settings) -> RunbookRetriever:
    store = InMemoryVectorStore(HashingEmbedding(dimensions=settings.embedding_dimensions))
    chunks = index_directory(store, settings.runbook_directory)
    log.info("rag.indexed", chunks=chunks, directory=str(settings.runbook_directory))
    return RunbookRetriever(store)


def build_registry(env: SimulatedEnvironment | None = None) -> ToolRegistry:
    """Read-only by construction — see :mod:`aic.tools.registry`."""
    return ToolRegistry(
        build_infrastructure_tools(env or demo_environment()),
        max_risk=RiskLevel.READ_ONLY,
    )


def build_agents(
    llm: LLMClient, registry: ToolRegistry, retriever: RunbookRetriever, policy: ActionPolicy
) -> Agents:
    return Agents(
        monitoring=MonitoringAgent(llm),
        diagnostic=DiagnosticAgent(llm),
        infrastructure=InfrastructureAgent(llm, registry),
        runbook=RunbookAgent(llm, retriever),
        action=ActionAgent(llm, policy),
    )


def build_container(
    settings: Settings,
    *,
    db_pool: Any | None = None,
    redis_client: Any | None = None,
    environment: SimulatedEnvironment | None = None,
) -> Container:
    """Assemble the application.

    ``db_pool`` and ``redis_client`` are optional: when absent, the in-memory
    adapters are used. That is what makes the test suite and the ``fake`` demo
    run with no services attached.
    """
    llm = build_llm(settings)
    policy = ActionPolicy(auto_approve_max_risk=settings.auto_approve_max_risk)
    registry = build_registry(environment)
    retriever = build_retriever(settings)

    repository: IncidentRepository = (
        PostgresIncidentRepository(db_pool)
        if db_pool is not None
        else InMemoryIncidentRepository()
    )
    checkpoints: CheckpointStore = (
        RedisCheckpointStore(redis_client)
        if redis_client is not None
        else InMemoryCheckpointStore()
    )

    agents = build_agents(llm, registry, retriever, policy)
    runner = InvestigationRunner(
        build_graph(agents),
        checkpoints=checkpoints,
        timeout_seconds=settings.investigation_timeout_seconds,
    )

    service = IncidentService(
        repository=repository,
        runner=runner,
        policy=policy,
        checkpoints=checkpoints,
    )

    log.info(
        "container.built",
        llm_provider=str(settings.llm_provider),
        repository=type(repository).__name__,
        checkpoints=type(checkpoints).__name__,
        tools=registry.names,
    )
    return Container(
        settings=settings,
        llm=llm,
        policy=policy,
        repository=repository,
        checkpoints=checkpoints,
        retriever=retriever,
        registry=registry,
        service=service,
    )
