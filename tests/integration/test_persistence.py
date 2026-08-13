"""Postgres repository and migration runner, against a real database.

Marked ``integration`` and excluded from the default run — start the services
with ``docker compose up -d postgres redis`` (or let CI do it) and run
``pytest -m integration``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from aic.config import Settings
from aic.domain.errors import NotFoundError
from aic.domain.models import Incident, IncidentStatus, Severity
from aic.infrastructure.db.migrations import discover, migrate
from aic.infrastructure.db.repository import PostgresIncidentRepository

pytestmark = pytest.mark.integration


@pytest.fixture
async def pool() -> AsyncIterator[object]:
    asyncpg = pytest.importorskip("asyncpg")
    settings = Settings()
    try:
        created = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
    except OSError as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Postgres is not reachable at {settings.database_url}: {exc}")
    try:
        await migrate(created)
        yield created
    finally:
        await created.close()


@pytest.fixture
async def repository(pool: object) -> AsyncIterator[PostgresIncidentRepository]:
    repo = PostgresIncidentRepository(pool)
    yield repo
    async with pool.acquire() as conn:  # type: ignore[attr-defined]
        await conn.execute("DELETE FROM incidents")


class TestMigrations:
    def test_every_migration_file_is_well_formed(self) -> None:
        migrations = discover()
        assert migrations
        assert [m.version for m in migrations] == sorted(m.version for m in migrations)

    async def test_migrations_are_idempotent(self, pool: object) -> None:
        """A second boot of a replica must not try to re-apply anything."""
        assert await migrate(pool) == []


class TestPostgresRepository:
    async def test_round_trips_an_incident(
        self, repository: PostgresIncidentRepository
    ) -> None:
        incident = Incident(title="checkout 5xx", severity=Severity.SEV2)
        await repository.save(incident)

        loaded = await repository.get(incident.id)
        assert loaded.id == incident.id
        assert loaded.severity is Severity.SEV2

    async def test_save_is_an_upsert(self, repository: PostgresIncidentRepository) -> None:
        incident = Incident(title="checkout 5xx")
        await repository.save(incident)

        incident.transition_to(IncidentStatus.MITIGATING)
        await repository.save(incident)

        assert (await repository.get(incident.id)).status is IncidentStatus.MITIGATING
        assert len(await repository.list()) == 1

    async def test_missing_incident_raises(
        self, repository: PostgresIncidentRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            await repository.get("does-not-exist")

    async def test_list_is_newest_first_and_respects_the_limit(
        self, repository: PostgresIncidentRepository
    ) -> None:
        for index in range(5):
            await repository.save(Incident(title=f"incident {index}"))

        listed = await repository.list(limit=3)
        assert len(listed) == 3
        assert [i.created_at for i in listed] == sorted(
            (i.created_at for i in listed), reverse=True
        )

    async def test_approvals_survive_the_round_trip(
        self, repository: PostgresIncidentRepository
    ) -> None:
        """The audit trail is the point; it must not be lost in serialization."""
        from aic.domain.models import ActionPlan, ApprovalDecision, ProposedAction

        action = ProposedAction(title="rollback", description="revert")
        incident = Incident(title="t", plan=ActionPlan(summary="s", actions=[action]))
        incident.record_decision(
            ApprovalDecision(action_id=action.id, approved=True, decided_by="sre-oncall")
        )
        await repository.save(incident)

        loaded = await repository.get(incident.id)
        decision = loaded.decision_for(action.id)
        assert decision is not None
        assert decision.decided_by == "sre-oncall"
        assert loaded.plan is not None
        assert loaded.plan.action(action.id) is not None
