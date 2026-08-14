"""The read-only inspection tools.

Every tool here is ``RiskLevel.READ_ONLY``, which is what allows the
Infrastructure Agent to run unattended: the registry it is given rejects
anything else at construction time, so no prompt can talk it into a mutation.

Mutating operations are never tools. They are emitted as
:class:`~aic.domain.models.ProposedAction` objects and go through the approval
flow instead — see :mod:`aic.guardrails.policy`.

The tools hold an :class:`~aic.tools.client.InfrastructureClient`, not a data
structure, so the same six tools serve the simulated source and a live AWS
account without changing a line here. What *does* live here is the description
the model reads, which is the part that decides whether a tool gets called at
the right moment.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aic.domain.errors import ToolExecutionError
from aic.tools.base import NoArgs, Tool
from aic.tools.client import (
    InfrastructureClient,
    SourceUnavailableError,
    UnknownResourceError,
)


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


class _ClientTool(Tool):
    """Base for tools that read through the infrastructure port."""

    def __init__(self, client: InfrastructureClient) -> None:
        self._client = client

    async def _call(self, operation: str, coro: Any) -> dict[str, Any]:
        """Translate source errors into messages the model can act on."""
        try:
            return await coro  # type: ignore[no-any-return]
        except UnknownResourceError as exc:
            raise ToolExecutionError(self.name, str(exc)) from exc
        except SourceUnavailableError as exc:
            raise ToolExecutionError(
                self.name,
                f"{operation} could not be read ({exc}). Treat this resource as "
                "unverified rather than assuming a value.",
            ) from exc


class ListActiveAlarms(_ClientTool):
    name = "list_active_alarms"
    description = (
        "List every alarm currently in ALARM state, with its metric, threshold, "
        "observed value, and how long it has been firing. Use this early to see "
        "the blast radius across services — one service failing and one service "
        "plus its dependencies failing are different incidents."
    )
    input_model = NoArgs

    async def run(self, args: NoArgs) -> dict[str, Any]:
        return await self._call("alarm state", self._client.list_active_alarms())


class ListRecentDeployments(_ClientTool):
    name = "list_recent_deployments"
    description = (
        "List recent deployments with revision, timestamp, and a summary of what "
        "changed. Use this to test whether an incident correlates with a release — "
        "the single most common root cause in practice."
    )
    input_model = NoArgs

    async def run(self, args: NoArgs) -> dict[str, Any]:
        return await self._call("deployment history", self._client.list_recent_deployments())


class DescribeEcsService(_ClientTool):
    name = "describe_ecs_service"
    description = (
        "Describe one ECS service: desired vs running task counts, CPU and memory "
        "utilization, current task definition, and the reasons recent tasks stopped. "
        "Use this first when a service is returning errors, to tell 'the app is "
        "broken' apart from 'the tasks are not staying up'."
    )
    input_model = ServiceArgs

    async def run(self, args: ServiceArgs) -> dict[str, Any]:
        return await self._call(
            f"ECS service {args.service!r}", self._client.describe_ecs_service(args.service)
        )


class DescribeRdsInstance(_ClientTool):
    name = "describe_rds_instance"
    description = (
        "Describe one RDS/Aurora instance: status, instance class, connection count "
        "against max_connections, CPU, and read/write latency. Use this when a "
        "service's errors could be database-side rather than application-side."
    )
    input_model = InstanceArgs

    async def run(self, args: InstanceArgs) -> dict[str, Any]:
        return await self._call(
            f"RDS instance {args.identifier!r}",
            self._client.describe_rds_instance(args.identifier),
        )


class DescribeCacheCluster(_ClientTool):
    name = "describe_cache_cluster"
    description = (
        "Describe one ElastiCache/Redis cluster: status, memory usage, evictions, "
        "and connected clients. Use this to rule the cache in or out as a cause of "
        "latency or error spikes."
    )
    input_model = ClusterArgs

    async def run(self, args: ClusterArgs) -> dict[str, Any]:
        return await self._call(
            f"cache cluster {args.cluster_id!r}",
            self._client.describe_cache_cluster(args.cluster_id),
        )


class DescribeQueue(_ClientTool):
    name = "describe_queue"
    description = (
        "Describe one SQS queue: visible and in-flight message counts, age of the "
        "oldest message, and redrive policy. Use this to detect consumer stalls and "
        "growing backlogs."
    )
    input_model = QueueArgs

    async def run(self, args: QueueArgs) -> dict[str, Any]:
        return await self._call(
            f"queue {args.queue_name!r}", self._client.describe_queue(args.queue_name)
        )


def build_inspection_tools(client: InfrastructureClient) -> list[Tool]:
    """Every read-only inspection tool, bound to one source."""
    return [
        ListActiveAlarms(client),
        ListRecentDeployments(client),
        DescribeEcsService(client),
        DescribeRdsInstance(client),
        DescribeCacheCluster(client),
        DescribeQueue(client),
    ]
