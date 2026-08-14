"""`aic doctor`.

Doctor exists to turn a misconfiguration into a readable sentence instead of a
stack trace during an incident, so the property that matters is: it never
raises, and a denial names the permission that would fix it.
"""

from __future__ import annotations

from typing import Any

import pytest

from aic.diagnostics import (
    REQUIRED_ACTIONS,
    Check,
    CheckStatus,
    check_source,
    missing_actions,
    run_checks,
)
from aic.manifest import Manifest, SimulatedSource
from aic.tools.client import SourceUnavailableError, UnknownResourceError
from aic.tools.simulated import SimulatedInfrastructure


class BrokenClient:
    """A source that fails every probe in a different way."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    @property
    def name(self) -> str:
        return "broken"

    async def list_active_alarms(self) -> dict[str, Any]:
        raise self._error

    async def list_recent_deployments(self) -> dict[str, Any]:
        raise self._error

    async def describe_ecs_service(self, service: str) -> dict[str, Any]:
        raise self._error

    async def describe_rds_instance(self, identifier: str) -> dict[str, Any]:
        raise self._error

    async def describe_cache_cluster(self, cluster_id: str) -> dict[str, Any]:
        raise self._error

    async def describe_queue(self, queue_name: str) -> dict[str, Any]:
        raise self._error


class TestProbes:
    async def test_a_working_source_reports_ok(self) -> None:
        checks = await check_source(SimulatedInfrastructure())
        assert checks
        assert all(c.status is CheckStatus.OK for c in checks)

    async def test_an_iam_denial_is_classified_as_denied(self) -> None:
        broken = BrokenClient(
            SourceUnavailableError(
                "IAM denied cloudwatch:describe_alarms — no identity-based policy"
            )
        )
        checks = await check_source(broken)

        assert {c.status for c in checks} == {CheckStatus.DENIED}
        assert "IAM denied" in checks[0].detail

    async def test_a_connection_failure_is_classified_as_unavailable(self) -> None:
        """Distinct from a denial: one you fix with IAM, the other with routing."""
        broken = BrokenClient(SourceUnavailableError("could not connect to endpoint"))
        checks = await check_source(broken)

        assert {c.status for c in checks} == {CheckStatus.UNAVAILABLE}

    async def test_an_unexpected_exception_is_reported_not_raised(self) -> None:
        """Doctor's whole job is producing a complete report."""
        checks = await check_source(BrokenClient(RuntimeError("something odd")))

        assert {c.status for c in checks} == {CheckStatus.UNAVAILABLE}
        assert "RuntimeError" in checks[0].detail

    async def test_reaching_the_api_and_finding_nothing_still_counts_as_ok(self) -> None:
        """An empty account is a working connection, not a broken one."""
        checks = await check_source(BrokenClient(UnknownResourceError("queue", "orders")))
        assert {c.status for c in checks} == {CheckStatus.OK}


class TestReport:
    async def test_the_simulated_manifest_passes_end_to_end(self) -> None:
        checks = await run_checks(Manifest(sources=[SimulatedSource()]))
        assert checks
        assert all(c.healthy for c in checks)

    def test_denials_are_translated_into_iam_actions(self) -> None:
        checks = [
            Check(source="aws", capability="alarms", status=CheckStatus.DENIED),
            Check(source="aws", capability="deployments", status=CheckStatus.DENIED),
            Check(source="aws", capability="rds", status=CheckStatus.OK),
        ]
        actions = missing_actions(checks)

        assert "cloudwatch:DescribeAlarms" in actions
        assert "ecs:DescribeServices" in actions
        # A passing check must not contribute a "missing" permission.
        assert "rds:DescribeDBInstances" not in actions

    def test_no_denials_means_nothing_to_add(self) -> None:
        healthy = [Check(source="s", capability="alarms", status=CheckStatus.OK)]
        assert missing_actions(healthy) == []

    @pytest.mark.parametrize("capability", sorted(REQUIRED_ACTIONS))
    def test_every_capability_maps_to_at_least_one_action(self, capability: str) -> None:
        """A denial with no known fix would leave the operator stuck."""
        assert REQUIRED_ACTIONS[capability]
        assert all(":" in action for action in REQUIRED_ACTIONS[capability])
