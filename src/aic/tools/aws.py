"""The AWS source: an `InfrastructureClient` backed by boto3.

Three things here are worth more than the API calls themselves.

**Errors that name the missing permission.** An `AccessDenied` that says only
"access denied" costs an operator twenty minutes. botocore's message already
carries the action that was refused, so we surface it and say plainly that this
is a gap in our access rather than a fact about the environment — which is what
stops the agent reasoning from a value it never actually read.

**Sync calls off the event loop.** boto3 is synchronous. Rather than take an
async AWS client as a dependency, calls go through `asyncio.to_thread`. At a
handful of calls per investigation that is the right trade: no extra dependency,
no client lifecycle to manage, and the loop stays free.

**Adaptive retries.** Throttling during an incident is exactly when you cannot
afford to give up, and it is also when everyone else is hammering the same APIs.

boto3 is an optional dependency — ``pip install "ai-incident-commander[aws]"``.
The import is local so the simulated path never pays for it.
"""

from __future__ import annotations

import asyncio
from functools import cached_property
from typing import Any

import structlog

from aic.tools.client import SourceUnavailableError, UnknownResourceError

log = structlog.get_logger(__name__)

#: botocore error codes that mean "this resource does not exist", per service.
_NOT_FOUND_CODES = frozenset(
    {
        "ServiceNotFoundException",
        "ClusterNotFoundException",
        "DBInstanceNotFound",
        "DBInstanceNotFoundFault",
        "DBClusterNotFoundFault",
        "CacheClusterNotFound",
        "CacheClusterNotFoundFault",
        "AWS.SimpleQueueService.NonExistentQueue",
        "QueueDoesNotExist",
        "ResourceNotFoundException",
    }
)

#: Codes that mean our IAM policy is short something.
_DENIED_CODES = frozenset(
    {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "NotAuthorized"}
)

#: How many recent alarm/deployment entries are worth putting in a prompt.
MAX_ALARMS = 40
MAX_DEPLOYMENTS = 20


class AwsInfrastructure:
    """Implements :class:`~aic.tools.client.InfrastructureClient` against AWS."""

    def __init__(
        self,
        *,
        region: str,
        profile: str | None = None,
        role_arn: str | None = None,
        max_attempts: int = 5,
    ) -> None:
        self._region = region
        self._profile = profile
        self._role_arn = role_arn
        self._max_attempts = max_attempts

    @property
    def name(self) -> str:
        target = self._role_arn or self._profile or "default credentials"
        return f"aws({self._region}, {target})"

    # -- session ----------------------------------------------------------

    @cached_property
    def _session(self) -> Any:
        """A boto3 session, assuming the configured role if there is one."""
        import boto3

        session = boto3.Session(profile_name=self._profile, region_name=self._region)
        if self._role_arn is None:
            return session

        # Cross-account: the incident commander lives outside the account it
        # watches, which is the arrangement you want anyway.
        sts = session.client("sts")
        assumed = sts.assume_role(
            RoleArn=self._role_arn, RoleSessionName="ai-incident-commander"
        )
        credentials = assumed["Credentials"]
        return boto3.Session(
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
            region_name=self._region,
        )

    def client(self, service: str) -> Any:
        """A boto3 client with adaptive retries configured."""
        from botocore.config import Config

        return self._session.client(
            service,
            config=Config(
                retries={"max_attempts": self._max_attempts, "mode": "adaptive"},
                user_agent_extra="ai-incident-commander",
            ),
        )

    async def _call(self, service: str, operation: str, **kwargs: Any) -> Any:
        """Run one boto3 call off the event loop, translating its failures."""
        from botocore.exceptions import BotoCoreError, ClientError

        def invoke() -> Any:
            return getattr(self.client(service), operation)(**kwargs)

        try:
            return await asyncio.to_thread(invoke)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            message = exc.response.get("Error", {}).get("Message", str(exc))
            if code in _NOT_FOUND_CODES:
                requested = str(kwargs.get("_requested", "resource"))
                raise UnknownResourceError(service, requested) from exc
            if code in _DENIED_CODES:
                log.warning(
                    "aws.access_denied", service=service, operation=operation, message=message
                )
                raise SourceUnavailableError(
                    f"IAM denied {service}:{operation} — {message}. "
                    f"Run `aic doctor` to see the full list of missing permissions."
                ) from exc
            raise SourceUnavailableError(
                f"{service}:{operation} failed [{code}] {message}"
            ) from exc
        except BotoCoreError as exc:
            # Credential resolution, endpoint, and connection problems land here.
            raise SourceUnavailableError(f"{service}:{operation} failed: {exc}") from exc

    # -- inspection -------------------------------------------------------

    async def list_active_alarms(self) -> dict[str, Any]:
        response = await self._call(
            "cloudwatch", "describe_alarms", StateValue="ALARM", MaxRecords=100
        )
        alarms = [
            {
                "alarmName": a.get("AlarmName"),
                "state": a.get("StateValue"),
                "metric": a.get("MetricName"),
                "namespace": a.get("Namespace"),
                "threshold": a.get("Threshold"),
                "since": _iso(a.get("StateUpdatedTimestamp")),
                "reason": a.get("StateReason"),
            }
            for a in response.get("MetricAlarms", [])
        ]
        alarms.sort(key=lambda a: str(a.get("since") or ""), reverse=True)
        return {"alarms": alarms[:MAX_ALARMS], "count": len(alarms)}

    async def list_recent_deployments(self) -> dict[str, Any]:
        """ECS keeps deployment state on the service, so walk the services."""
        deployments: list[dict[str, Any]] = []

        for cluster in await self._clusters():
            services = await self._service_arns(cluster)
            for chunk in _chunks(services, 10):  # describe_services caps at 10
                described = await self._call(
                    "ecs", "describe_services", cluster=cluster, services=chunk
                )
                for service in described.get("services", []):
                    for deployment in service.get("deployments", []):
                        deployments.append(
                            {
                                "service": service.get("serviceName"),
                                "cluster": cluster,
                                "revision": deployment.get("taskDefinition", "").rsplit("/", 1)[-1],
                                "status": deployment.get("status"),
                                "deployedAt": _iso(deployment.get("createdAt")),
                                "rolloutState": deployment.get("rolloutState"),
                                "rolloutStateReason": deployment.get("rolloutStateReason"),
                                "runningCount": deployment.get("runningCount"),
                                "desiredCount": deployment.get("desiredCount"),
                            }
                        )

        deployments.sort(key=lambda d: str(d.get("deployedAt") or ""), reverse=True)
        return {"deployments": deployments[:MAX_DEPLOYMENTS], "count": len(deployments)}

    async def describe_ecs_service(self, service: str) -> dict[str, Any]:
        for cluster in await self._clusters():
            described = await self._call(
                "ecs", "describe_services", cluster=cluster, services=[service]
            )
            for found in described.get("services", []):
                return await self._ecs_detail(cluster, found)

        raise UnknownResourceError("ECS service", service, known=await self._all_service_names())

    async def _ecs_detail(self, cluster: str, service: dict[str, Any]) -> dict[str, Any]:
        """Service state plus why its recent tasks stopped, which is the tell."""
        stopped_reasons: list[str] = []
        try:
            stopped = await self._call(
                "ecs",
                "list_tasks",
                cluster=cluster,
                serviceName=service["serviceName"],
                desiredStatus="STOPPED",
                maxResults=10,
            )
            arns = stopped.get("taskArns", [])
            if arns:
                tasks = await self._call("ecs", "describe_tasks", cluster=cluster, tasks=arns)
                stopped_reasons = [
                    reason
                    for task in tasks.get("tasks", [])
                    if (reason := task.get("stoppedReason"))
                ]
        except SourceUnavailableError as exc:
            # Losing stop reasons degrades the answer; it should not lose the
            # task counts we already have.
            stopped_reasons = [f"(stop reasons unavailable: {exc})"]

        return {
            "cluster": cluster,
            "serviceName": service.get("serviceName"),
            "status": service.get("status"),
            "desiredCount": service.get("desiredCount"),
            "runningCount": service.get("runningCount"),
            "pendingCount": service.get("pendingCount"),
            "taskDefinition": service.get("taskDefinition", "").rsplit("/", 1)[-1],
            "recentTaskStoppedReasons": stopped_reasons,
            "events": [e.get("message") for e in service.get("events", [])[:5]],
        }

    async def describe_rds_instance(self, identifier: str) -> dict[str, Any]:
        response = await self._call(
            "rds", "describe_db_instances", DBInstanceIdentifier=identifier
        )
        instances = response.get("DBInstances", [])
        if not instances:
            raise UnknownResourceError("RDS instance", identifier)
        instance = instances[0]

        metrics = await self._metrics(
            namespace="AWS/RDS",
            dimensions=[{"Name": "DBInstanceIdentifier", "Value": identifier}],
            names=("DatabaseConnections", "CPUUtilization", "ReadLatency", "WriteLatency"),
        )
        return {
            "engine": instance.get("Engine"),
            "status": instance.get("DBInstanceStatus"),
            "instanceClass": instance.get("DBInstanceClass"),
            "multiAZ": instance.get("MultiAZ"),
            "currentConnections": metrics.get("DatabaseConnections"),
            "cpuUtilization": metrics.get("CPUUtilization"),
            "readLatencyMs": _to_ms(metrics.get("ReadLatency")),
            "writeLatencyMs": _to_ms(metrics.get("WriteLatency")),
        }

    async def describe_cache_cluster(self, cluster_id: str) -> dict[str, Any]:
        response = await self._call(
            "elasticache", "describe_cache_clusters", CacheClusterId=cluster_id
        )
        clusters = response.get("CacheClusters", [])
        if not clusters:
            raise UnknownResourceError("cache cluster", cluster_id)
        cluster = clusters[0]

        metrics = await self._metrics(
            namespace="AWS/ElastiCache",
            dimensions=[{"Name": "CacheClusterId", "Value": cluster_id}],
            names=(
                "Evictions",
                "CPUUtilization",
                "DatabaseMemoryUsagePercentage",
                "CurrConnections",
            ),
        )
        return {
            "engine": cluster.get("Engine"),
            "status": cluster.get("CacheClusterStatus"),
            "nodeType": cluster.get("CacheNodeType"),
            "evictionsLastHour": metrics.get("Evictions"),
            "cpuUtilization": metrics.get("CPUUtilization"),
            "memoryUsagePercent": metrics.get("DatabaseMemoryUsagePercentage"),
            "connectedClients": metrics.get("CurrConnections"),
        }

    async def describe_queue(self, queue_name: str) -> dict[str, Any]:
        url = await self._call("sqs", "get_queue_url", QueueName=queue_name)
        attributes = await self._call(
            "sqs", "get_queue_attributes", QueueUrl=url["QueueUrl"], AttributeNames=["All"]
        )
        attrs = attributes.get("Attributes", {})
        return {
            "approximateNumberOfMessages": _to_int(attrs.get("ApproximateNumberOfMessages")),
            "approximateNumberOfMessagesNotVisible": _to_int(
                attrs.get("ApproximateNumberOfMessagesNotVisible")
            ),
            "visibilityTimeout": _to_int(attrs.get("VisibilityTimeout")),
            "redrivePolicy": attrs.get("RedrivePolicy"),
        }

    # -- helpers ----------------------------------------------------------

    async def _clusters(self) -> list[str]:
        response = await self._call("ecs", "list_clusters")
        return [arn.rsplit("/", 1)[-1] for arn in response.get("clusterArns", [])]

    async def _service_arns(self, cluster: str) -> list[str]:
        response = await self._call("ecs", "list_services", cluster=cluster, maxResults=100)
        return list(response.get("serviceArns", []))

    async def _all_service_names(self) -> list[str]:
        names: list[str] = []
        for cluster in await self._clusters():
            names.extend(arn.rsplit("/", 1)[-1] for arn in await self._service_arns(cluster))
        return names

    async def _metrics(
        self, *, namespace: str, dimensions: list[dict[str, str]], names: tuple[str, ...]
    ) -> dict[str, float | None]:
        """Latest datapoint per metric over the last hour, in one API call."""
        from datetime import UTC, datetime, timedelta

        end = datetime.now(UTC)
        queries = [
            {
                "Id": f"m{index}",
                "MetricStat": {
                    "Metric": {
                        "Namespace": namespace,
                        "MetricName": name,
                        "Dimensions": dimensions,
                    },
                    "Period": 300,
                    "Stat": "Average",
                },
                "ReturnData": True,
            }
            for index, name in enumerate(names)
        ]

        try:
            response = await self._call(
                "cloudwatch",
                "get_metric_data",
                MetricDataQueries=queries,
                StartTime=end - timedelta(hours=1),
                EndTime=end,
                ScanBy="TimestampDescending",
            )
        except SourceUnavailableError as exc:
            log.warning("aws.metrics_unavailable", namespace=namespace, error=str(exc))
            return dict.fromkeys(names)

        latest: dict[str, float | None] = {}
        for index, name in enumerate(names):
            series = next(
                (r for r in response.get("MetricDataResults", []) if r["Id"] == f"m{index}"),
                None,
            )
            values = (series or {}).get("Values") or []
            latest[name] = round(float(values[0]), 3) if values else None
        return latest


def _iso(value: Any) -> str | None:
    """boto3 hands back datetimes; everything downstream wants a string."""
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value) if value is not None else None


def _to_ms(seconds: float | None) -> float | None:
    return round(seconds * 1000, 2) if seconds is not None else None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]
