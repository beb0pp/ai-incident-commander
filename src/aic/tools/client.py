"""The infrastructure port.

This is the seam that makes a new environment a configuration change rather than
a code change. The tools call these six methods; where the answers come from —
a fixture, AWS, something else entirely — is somebody else's problem.

Adding a source means implementing this protocol and registering it in the
manifest. It does not mean touching the tools, the registry, the agents, or the
prompts.

Return shapes are boto3's, because they are already the lingua franca for this
domain and because a simulated source that lies about the shape is not a useful
rehearsal for the real one.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class InfrastructureClient(Protocol):
    """Read-only inspection of the components an incident might involve."""

    @property
    def name(self) -> str:
        """Identifies this source in logs and in `aic doctor` output."""
        ...

    async def list_active_alarms(self) -> dict[str, Any]:
        """Every alarm currently firing, with metric, threshold, and value."""
        ...

    async def list_recent_deployments(self) -> dict[str, Any]:
        """Recent releases with revision, timestamp, and what changed."""
        ...

    async def describe_ecs_service(self, service: str) -> dict[str, Any]:
        """Task counts, utilization, task definition, recent stop reasons."""
        ...

    async def describe_rds_instance(self, identifier: str) -> dict[str, Any]:
        """Status, connection count against the ceiling, CPU, latency."""
        ...

    async def describe_cache_cluster(self, cluster_id: str) -> dict[str, Any]:
        """Status, memory usage, evictions, connected clients."""
        ...

    async def describe_queue(self, queue_name: str) -> dict[str, Any]:
        """Depth, in-flight count, age of the oldest message, redrive policy."""
        ...


class UnknownResourceError(Exception):
    """A resource was requested that this source does not have.

    Carries the names that *do* exist where the source can enumerate them
    cheaply. An error the model can act on beats one it can only report — it
    will retry with a real name instead of giving up or inventing one.
    """

    def __init__(self, kind: str, requested: str, known: list[str] | None = None) -> None:
        message = f"no {kind} named {requested!r}"
        if known:
            message += f"; known {kind}s: {sorted(known)}"
        super().__init__(message)
        self.kind = kind
        self.requested = requested
        self.known = known or []


class SourceUnavailableError(Exception):
    """The source could not be reached or refused the request.

    Distinct from :class:`UnknownResourceError` on purpose: a missing resource
    is a fact about the environment, while this is a fact about our access to
    it. The first is an answer; the second is a gap, and the Infrastructure
    Agent is told to report it as one rather than assume a value.
    """
