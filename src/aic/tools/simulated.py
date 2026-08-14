"""The simulated source: an `InfrastructureClient` over an in-memory snapshot.

Keeps the project runnable with no cloud account, and gives the test suite a
source whose answers are fixed. It returns boto3-shaped payloads so it is a
real rehearsal for the AWS source rather than a friendlier fiction.
"""

from __future__ import annotations

from typing import Any

from aic.tools.client import UnknownResourceError
from aic.tools.environment import SimulatedEnvironment, demo_environment


class SimulatedInfrastructure:
    """Implements :class:`~aic.tools.client.InfrastructureClient` from a snapshot."""

    def __init__(self, environment: SimulatedEnvironment | None = None) -> None:
        self._env = environment or demo_environment()

    @property
    def name(self) -> str:
        return "simulated"

    async def list_active_alarms(self) -> dict[str, Any]:
        firing = [a for a in self._env.alarms if a.get("state") == "ALARM"]
        return {"alarms": firing, "count": len(firing)}

    async def list_recent_deployments(self) -> dict[str, Any]:
        ordered = sorted(
            self._env.deployments, key=lambda d: str(d.get("deployedAt", "")), reverse=True
        )
        return {"deployments": ordered, "count": len(ordered)}

    async def describe_ecs_service(self, service: str) -> dict[str, Any]:
        return self._lookup("ECS service", self._env.ecs_services, service)

    async def describe_rds_instance(self, identifier: str) -> dict[str, Any]:
        return self._lookup("RDS instance", self._env.rds_instances, identifier)

    async def describe_cache_cluster(self, cluster_id: str) -> dict[str, Any]:
        return self._lookup("cache cluster", self._env.cache_clusters, cluster_id)

    async def describe_queue(self, queue_name: str) -> dict[str, Any]:
        return self._lookup("queue", self._env.queues, queue_name)

    @staticmethod
    def _lookup(kind: str, table: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
        found = table.get(key)
        if found is None:
            raise UnknownResourceError(kind, key, known=list(table))
        return found
