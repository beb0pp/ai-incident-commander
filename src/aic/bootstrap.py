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
from aic.guardrails.policy import ActionPolicy
from aic.infrastructure.db.repository import (
    IncidentRepository,
    InMemoryIncidentRepository,
    PostgresIncidentRepository,
)
from aic.llm.base import LLMClient
from aic.manifest import Manifest, SimulatedSource, Source, load_manifest
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
from aic.tools.client import InfrastructureClient
from aic.tools.inspection import build_inspection_tools
from aic.tools.registry import ToolRegistry
from aic.tools.simulated import SimulatedInfrastructure

log = structlog.get_logger(__name__)


@dataclass
class Container:
    """Everything the application needs, already wired together."""

    settings: Settings
    manifest: Manifest
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


def build_retriever(settings: Settings, manifest: Manifest) -> RunbookRetriever:
    """Index every runbook location the manifest names, plus the settings default."""
    store = InMemoryVectorStore(HashingEmbedding(dimensions=settings.embedding_dimensions))

    locations = [location.path for location in manifest.runbooks] or [
        settings.runbook_directory
    ]
    total = 0
    for path in locations:
        total += index_directory(store, path)
        log.info("rag.indexed", directory=str(path), chunks=total)

    return RunbookRetriever(store)


def build_client(source: Source) -> InfrastructureClient:
    """Turn one manifest source into a live infrastructure client."""
    if isinstance(source, SimulatedSource):
        return SimulatedInfrastructure()

    # Imported here so the simulated path never requires the [aws] extra.
    from aic.tools.aws import AwsInfrastructure

    return AwsInfrastructure(
        region=source.region,
        profile=source.profile,
        role_arn=source.role_arn,
        max_attempts=source.max_attempts,
    )


def build_registry(
    manifest: Manifest, *, client: InfrastructureClient | None = None
) -> ToolRegistry:
    """Build the tool registry under the ceiling the manifest declares.

    The ceiling is data now rather than a constant, but it is still enforced
    where it always was: `ToolRegistry` refuses at construction time. Making
    configuration able to *raise* the ceiling would be the mistake; making it
    able to *declare* one that the registry then enforces is not.
    """
    resolved = client or build_client(manifest.sources[0])
    registry = ToolRegistry(
        build_inspection_tools(resolved), max_risk=manifest.effective_ceiling
    )
    log.info(
        "tools.registered",
        source=resolved.name,
        ceiling=str(manifest.effective_ceiling),
        tools=registry.names,
    )
    return registry


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
    manifest: Manifest | None = None,
    db_pool: Any | None = None,
    redis_client: Any | None = None,
    client: InfrastructureClient | None = None,
) -> Container:
    """Assemble the application.

    ``manifest`` describes what this installation is connected to; when absent
    it is loaded from `aic.yaml`, and when that is absent too it falls back to
    the simulated source. ``db_pool`` and ``redis_client`` are optional in the
    same spirit — the in-memory adapters are used when they are not supplied,
    which is what lets the tests and the demo run with no services attached.
    """
    resolved_manifest = manifest or load_manifest(settings.manifest_path)

    llm = build_llm(settings)
    policy = ActionPolicy(auto_approve_max_risk=settings.auto_approve_max_risk)
    registry = build_registry(resolved_manifest, client=client)
    retriever = build_retriever(settings, resolved_manifest)

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
        sources=[s.type for s in resolved_manifest.sources],
        repository=type(repository).__name__,
        checkpoints=type(checkpoints).__name__,
        tools=registry.names,
    )
    return Container(
        settings=settings,
        manifest=resolved_manifest,
        llm=llm,
        policy=policy,
        repository=repository,
        checkpoints=checkpoints,
        retriever=retriever,
        registry=registry,
        service=service,
    )
