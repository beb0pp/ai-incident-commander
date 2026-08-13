"""A simulated AWS-shaped environment the infrastructure tools read from.

This is deliberately synthetic. The point of the project is the agent
architecture — orchestration, tool contracts, guardrails, evidence — not a real
cloud integration, and a synthetic environment keeps the repo runnable by anyone
who clones it with no account, no credentials, and no cost.

The shape mirrors what ``boto3`` actually returns for ECS / RDS / ElastiCache /
SQS / CloudWatch, so replacing :class:`SimulatedEnvironment` with a real client
is a change to one class rather than a change to the agents.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class SimulatedEnvironment:
    """An in-memory infrastructure snapshot addressed by resource name."""

    ecs_services: dict[str, dict[str, Any]] = field(default_factory=dict)
    rds_instances: dict[str, dict[str, Any]] = field(default_factory=dict)
    cache_clusters: dict[str, dict[str, Any]] = field(default_factory=dict)
    queues: dict[str, dict[str, Any]] = field(default_factory=dict)
    alarms: list[dict[str, Any]] = field(default_factory=list)
    deployments: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_json(cls, path: str | Path) -> SimulatedEnvironment:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ecs_services": self.ecs_services,
            "rds_instances": self.rds_instances,
            "cache_clusters": self.cache_clusters,
            "queues": self.queues,
            "alarms": self.alarms,
            "deployments": self.deployments,
        }


def demo_environment() -> SimulatedEnvironment:
    """The environment behind the bundled demo incident.

    The scenario: ``checkout-api`` started returning 5xx shortly after a deploy.
    The real cause is connection-pool exhaustion on Aurora, triggered by a
    config change in that deploy — visible only by correlating the ECS task
    restarts, the RDS connection count, and the deployment timeline. Any single
    signal on its own points somewhere misleading, which is the whole reason a
    correlating agent earns its place.
    """
    return SimulatedEnvironment(
        ecs_services={
            "checkout-api": {
                "cluster": "prod-cluster",
                "serviceName": "checkout-api",
                "status": "ACTIVE",
                "desiredCount": 12,
                "runningCount": 8,
                "pendingCount": 4,
                "cpuUtilization": 34.2,
                "memoryUtilization": 61.0,
                "recentTaskStoppedReasons": [
                    "Essential container in task exited",
                    "Task failed ELB health checks in target-group checkout-tg",
                ],
                "taskDefinition": "checkout-api:412",
            },
            "cart-api": {
                "cluster": "prod-cluster",
                "serviceName": "cart-api",
                "status": "ACTIVE",
                "desiredCount": 6,
                "runningCount": 6,
                "pendingCount": 0,
                "cpuUtilization": 22.5,
                "memoryUtilization": 48.1,
                "recentTaskStoppedReasons": [],
                "taskDefinition": "cart-api:88",
            },
        },
        rds_instances={
            "prod-aurora-orders": {
                "engine": "aurora-postgresql",
                "status": "available",
                "instanceClass": "db.r6g.xlarge",
                "maxConnections": 200,
                "currentConnections": 199,
                "cpuUtilization": 41.0,
                "readLatencyMs": 3.1,
                "writeLatencyMs": 148.7,
                "deadlocksLastHour": 0,
            }
        },
        cache_clusters={
            "prod-redis-sessions": {
                "engine": "redis",
                "status": "available",
                "nodeType": "cache.r6g.large",
                "evictionsLastHour": 0,
                "cpuUtilization": 18.4,
                "memoryUsagePercent": 52.0,
                "connectedClients": 340,
            }
        },
        queues={
            "orders-events": {
                "approximateNumberOfMessages": 18432,
                "approximateNumberOfMessagesNotVisible": 210,
                "approximateAgeOfOldestMessageSeconds": 1870,
                "redrivePolicy": {"deadLetterTargetArn": "orders-events-dlq", "maxReceiveCount": 5},
            }
        },
        alarms=[
            {
                "alarmName": "checkout-api-5xx-rate",
                "state": "ALARM",
                "metric": "HTTPCode_Target_5XX_Count",
                "threshold": 25,
                "value": 412,
                "since": "2026-08-13T14:02:00Z",
            },
            {
                "alarmName": "aurora-orders-connections",
                "state": "ALARM",
                "metric": "DatabaseConnections",
                "threshold": 180,
                "value": 199,
                "since": "2026-08-13T14:01:00Z",
            },
            {
                "alarmName": "cart-api-latency-p99",
                "state": "OK",
                "metric": "TargetResponseTime",
                "threshold": 1.5,
                "value": 0.42,
                "since": "2026-08-12T09:00:00Z",
            },
        ],
        deployments=[
            {
                "service": "checkout-api",
                "revision": "checkout-api:412",
                "deployedAt": "2026-08-13T13:54:00Z",
                "changeSummary": "raise DB pool size per task from 5 to 20",
                "deployedBy": "ci-pipeline",
            },
            {
                "service": "cart-api",
                "revision": "cart-api:88",
                "deployedAt": "2026-08-11T10:12:00Z",
                "changeSummary": "dependency bump",
                "deployedBy": "ci-pipeline",
            },
        ],
    )
