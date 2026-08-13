"""Incident persistence.

The port is defined here and implemented twice: in memory (tests, and the
zero-dependency demo) and on Postgres. Application code depends on the protocol,
so the test suite never needs a database running.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from aic.domain.errors import NotFoundError
from aic.domain.models import Incident


class IncidentRepository(Protocol):
    async def save(self, incident: Incident) -> None: ...

    async def get(self, incident_id: str) -> Incident: ...

    async def list(self, *, limit: int = 50) -> list[Incident]: ...


class InMemoryIncidentRepository:
    """Process-local storage. Deep-copies on read so callers cannot mutate state
    they do not own — the same isolation a real database gives you for free."""

    def __init__(self) -> None:
        self._items: dict[str, str] = {}

    async def save(self, incident: Incident) -> None:
        self._items[incident.id] = incident.model_dump_json()

    async def get(self, incident_id: str) -> Incident:
        raw = self._items.get(incident_id)
        if raw is None:
            raise NotFoundError(f"incident {incident_id!r} not found")
        return Incident.model_validate_json(raw)

    async def list(self, *, limit: int = 50) -> list[Incident]:
        incidents = [Incident.model_validate_json(raw) for raw in self._items.values()]
        incidents.sort(key=lambda i: i.created_at, reverse=True)
        return incidents[:limit]

    def __len__(self) -> int:
        return len(self._items)


class PostgresIncidentRepository:
    """asyncpg-backed storage.

    The incident aggregate is stored as one JSONB document rather than shredded
    across normalized tables. It is read and written whole, it has no
    cross-aggregate queries, and its shape is still moving — a document is the
    honest model here. The indexed columns exist so the common operational
    filters (open incidents, by severity, most recent) stay index-backed.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def save(self, incident: Incident) -> None:
        payload = incident.model_dump(mode="json")
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO incidents (id, status, severity, created_at, updated_at, document)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                ON CONFLICT (id) DO UPDATE SET
                    status     = EXCLUDED.status,
                    severity   = EXCLUDED.severity,
                    updated_at = EXCLUDED.updated_at,
                    document   = EXCLUDED.document
                """,
                incident.id,
                str(incident.status),
                str(incident.severity),
                incident.created_at,
                incident.updated_at,
                json.dumps(payload),
            )

    async def get(self, incident_id: str) -> Incident:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT document FROM incidents WHERE id = $1", incident_id
            )
        if row is None:
            raise NotFoundError(f"incident {incident_id!r} not found")
        return Incident.model_validate_json(row["document"])

    async def list(self, *, limit: int = 50) -> list[Incident]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT document FROM incidents ORDER BY created_at DESC LIMIT $1", limit
            )
        return [Incident.model_validate_json(row["document"]) for row in rows]
