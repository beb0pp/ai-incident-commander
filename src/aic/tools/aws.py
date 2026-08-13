"""Read-only infrastructure tools over the simulated AWS environment.

Every tool here is ``RiskLevel.READ_ONLY``, which is what allows the Infrastructure
Agent to run unattended: the registry it is given rejects anything else at
construction time, so no prompt can talk it into a mutation.

Mutating operations are never tools. They are emitted as
:class:`~aic.domain.models.ProposedAction` objects and go through the approval
flow instead — see :mod:`aic.guardrails.policy`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aic.domain.errors import ToolExecutionError
from aic.tools.base import NoArgs, Tool
from aic.tools.environment import SimulatedEnvironment


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServiceArgs(_Args):
    service: str = Field(description="ECS service name, e.g. 'checkout-api'.")


class InstanceArgs(_Args):
    identifier: str = Field(description="RDS instance identifier, e.g. 'prod-aurora-orders'.")


class ClusterArgs(_Args):
    cluster_id: str = Field(description="ElastiCache cluster id, e.g. 'prod-redis-sessions'.")


class QueueArgs(_Args):
    queue_name: str = Field(description="SQS queue name, e.g. 'orders-events'.")


class _EnvTool(Tool):
    """Base for tools that read the shared environment snapshot."""

    def __init__(self, env: SimulatedEnvironment) -> None:
        self._env = env


class DescribeEcsService(_EnvTool):
    name = "describe_ecs_service"
    description = (
        "Describe one ECS service: desired vs running task counts, CPU and memory "
        "utilization, current task definition, and the reasons recent tasks stopped. "
        "Use this first when a service is returning errors, to tell 'the app is "
        "broken' apart from 'the tasks are not staying up'."
    )
    input_model = ServiceArgs

    async def run(self, args: ServiceArgs) -> dict[str, Any]:
        service = self._env.ecs_services.get(args.service)
        if service is None:
            raise ToolExecutionError(
                self.name,
                f"no ECS service named {args.service!r}; "
                f"known services: {sorted(self._env.ecs_services)}",
            )
        return service


class DescribeRdsInstance(_EnvTool):
    name = "describe_rds_instance"
    description = (
        "Describe one RDS/Aurora instance: status, instance class, connection count "
        "against max_connections, CPU, and read/write latency. Use this when a "
        "service's errors could be database-side rather than application-side."
    )
    input_model = InstanceArgs

    async def run(self, args: InstanceArgs) -> dict[str, Any]:
        instance = self._env.rds_instances.get(args.identifier)
        if instance is None:
            raise ToolExecutionError(
                self.name,
                f"no RDS instance named {args.identifier!r}; "
                f"known instances: {sorted(self._env.rds_instances)}",
            )
        return instance


class DescribeCacheCluster(_EnvTool):
    name = "describe_cache_cluster"
    description = (
        "Describe one ElastiCache/Redis cluster: status, memory usage, evictions, "
        "and connected clients. Use this to rule the cache in or out as a cause of "
        "latency or error spikes."
    )
    input_model = ClusterArgs

    async def run(self, args: ClusterArgs) -> dict[str, Any]:
        cluster = self._env.cache_clusters.get(args.cluster_id)
        if cluster is None:
            raise ToolExecutionError(
                self.name,
                f"no cache cluster named {args.cluster_id!r}; "
                f"known clusters: {sorted(self._env.cache_clusters)}",
            )
        return cluster


class DescribeQueue(_EnvTool):
    name = "describe_queue"
    description = (
        "Describe one SQS queue: visible and in-flight message counts, age of the "
        "oldest message, and redrive policy. Use this to detect consumer stalls and "
        "growing backlogs."
    )
    input_model = QueueArgs

    async def run(self, args: QueueArgs) -> dict[str, Any]:
        queue = self._env.queues.get(args.queue_name)
        if queue is None:
            raise ToolExecutionError(
                self.name,
                f"no queue named {args.queue_name!r}; known queues: {sorted(self._env.queues)}",
            )
        return queue


class ListActiveAlarms(_EnvTool):
    name = "list_active_alarms"
    description = (
        "List every CloudWatch alarm currently in ALARM state, with its metric, "
        "threshold, observed value, and how long it has been firing. Use this early "
        "to see the blast radius across services."
    )
    input_model = NoArgs

    async def run(self, args: NoArgs) -> dict[str, Any]:
        firing = [a for a in self._env.alarms if a.get("state") == "ALARM"]
        return {"alarms": firing, "count": len(firing)}


class ListRecentDeployments(_EnvTool):
    name = "list_recent_deployments"
    description = (
        "List recent deployments with revision, timestamp, and a summary of what "
        "changed. Use this to test whether an incident correlates with a release — "
        "the single most common root cause in practice."
    )
    input_model = NoArgs

    async def run(self, args: NoArgs) -> dict[str, Any]:
        ordered = sorted(
            self._env.deployments, key=lambda d: str(d.get("deployedAt", "")), reverse=True
        )
        return {"deployments": ordered, "count": len(ordered)}


def build_infrastructure_tools(env: SimulatedEnvironment) -> list[Tool]:
    """Every read-only infrastructure tool, bound to one environment snapshot."""
    return [
        ListActiveAlarms(env),
        ListRecentDeployments(env),
        DescribeEcsService(env),
        DescribeRdsInstance(env),
        DescribeCacheCluster(env),
        DescribeQueue(env),
    ]
