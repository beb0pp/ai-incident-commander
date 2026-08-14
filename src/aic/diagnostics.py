"""`aic doctor` — tell the operator what is wrong before an incident does.

The difference between a tool that is easy to adopt and one that is not is
usually this: when it is misconfigured, does it say so clearly, or does it fail
somewhere deep at the worst possible moment?

Doctor probes each configured source with the cheapest call per API and reports
one line per capability: reachable, denied (with the IAM action to add), or
unavailable. Nothing here mutates anything, and a failed check never raises —
the whole point is to produce a complete report rather than stop at the first
problem.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from aic.manifest import AwsSource, Manifest
from aic.tools.client import InfrastructureClient, SourceUnavailableError, UnknownResourceError


class CheckStatus(StrEnum):
    OK = "ok"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class Check:
    """One capability probe."""

    source: str
    capability: str
    status: CheckStatus
    detail: str = ""

    @property
    def healthy(self) -> bool:
        return self.status in (CheckStatus.OK, CheckStatus.SKIPPED)


#: Probe name -> the IAM actions it needs, so a denial names its own fix.
REQUIRED_ACTIONS: dict[str, tuple[str, ...]] = {
    "alarms": ("cloudwatch:DescribeAlarms",),
    "metrics": ("cloudwatch:GetMetricData",),
    "deployments": ("ecs:ListClusters", "ecs:ListServices", "ecs:DescribeServices"),
    "ecs": ("ecs:ListClusters", "ecs:DescribeServices", "ecs:ListTasks", "ecs:DescribeTasks"),
    "rds": ("rds:DescribeDBInstances",),
    "elasticache": ("elasticache:DescribeCacheClusters",),
    "sqs": ("sqs:GetQueueUrl", "sqs:GetQueueAttributes"),
}


async def check_source(client: InfrastructureClient) -> list[Check]:
    """Probe one source. Never raises."""
    probes: list[tuple[str, Callable[[], Awaitable[Any]]]] = [
        ("alarms", client.list_active_alarms),
        ("deployments", client.list_recent_deployments),
    ]

    results = await asyncio.gather(
        *(_probe(client.name, name, call) for name, call in probes)
    )
    return list(results)


async def _probe(
    source: str, capability: str, call: Callable[[], Awaitable[Any]]
) -> Check:
    try:
        await call()
    except SourceUnavailableError as exc:
        message = str(exc)
        status = CheckStatus.DENIED if "IAM denied" in message else CheckStatus.UNAVAILABLE
        return Check(source=source, capability=capability, status=status, detail=message)
    except UnknownResourceError as exc:
        # Reaching the API and finding nothing is a working connection.
        return Check(
            source=source, capability=capability, status=CheckStatus.OK, detail=str(exc)
        )
    except Exception as exc:  # doctor reports problems; it does not become one
        return Check(
            source=source,
            capability=capability,
            status=CheckStatus.UNAVAILABLE,
            detail=f"{type(exc).__name__}: {exc}",
        )
    return Check(source=source, capability=capability, status=CheckStatus.OK)


async def check_identity(source: AwsSource) -> Check:
    """Who are we, as far as AWS is concerned? The first question worth answering."""
    from aic.tools.aws import AwsInfrastructure

    infrastructure = AwsInfrastructure(
        region=source.region, profile=source.profile, role_arn=source.role_arn
    )
    name = infrastructure.name

    def whoami() -> Any:
        return infrastructure.client("sts").get_caller_identity()

    try:
        identity = await asyncio.to_thread(whoami)
    except Exception as exc:  # a credential problem is a report line, not a crash
        return Check(
            source=name,
            capability="credentials",
            status=CheckStatus.UNAVAILABLE,
            detail=(
                f"{type(exc).__name__}: {exc}. In a deployment this usually means the "
                "task role or instance profile is not attached."
            ),
        )

    return Check(
        source=name,
        capability="credentials",
        status=CheckStatus.OK,
        detail=f"{identity.get('Arn')} (account {identity.get('Account')})",
    )


async def run_checks(manifest: Manifest) -> list[Check]:
    """Probe every source in the manifest."""
    from aic.bootstrap import build_client

    checks: list[Check] = []
    for source in manifest.sources:
        if isinstance(source, AwsSource):
            identity = await check_identity(source)
            checks.append(identity)
            if not identity.healthy:
                # Without credentials every downstream probe reports the same
                # thing, which buries the one line that matters.
                checks.append(
                    Check(
                        source=identity.source,
                        capability="permissions",
                        status=CheckStatus.SKIPPED,
                        detail="skipped: credentials could not be resolved",
                    )
                )
                continue

        checks.extend(await check_source(build_client(source)))

    return checks


def missing_actions(checks: list[Check]) -> list[str]:
    """The IAM actions to add, derived from what was denied."""
    needed: set[str] = set()
    for check in checks:
        if check.status is CheckStatus.DENIED:
            needed.update(REQUIRED_ACTIONS.get(check.capability, ()))
    return sorted(needed)
