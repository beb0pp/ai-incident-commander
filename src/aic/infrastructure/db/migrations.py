"""A minimal forward-only SQL migration runner.

Alembic is the right answer once there is an ORM and a branching schema history.
There is neither here: the schema is two tables of JSONB, and a numbered
directory of ``.sql`` files applied inside a transaction with an advisory lock is
the whole requirement. See ``docs/adr/0005``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, NamedTuple

import structlog

log = structlog.get_logger(__name__)

# src/aic/infrastructure/db/migrations.py -> repository root
MIGRATIONS_DIR = Path(__file__).resolve().parents[4] / "migrations"

#: Any integer works; it just has to be the same one in every process.
_ADVISORY_LOCK_ID = 0x41_49_43_01

_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


class Migration(NamedTuple):
    version: int
    name: str
    sql: str


def discover(directory: Path | str = MIGRATIONS_DIR) -> list[Migration]:
    """Load and order every migration file, rejecting duplicate versions."""
    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {root}")

    found: dict[int, Migration] = {}
    for path in sorted(root.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if match is None:
            raise ValueError(
                f"migration {path.name!r} does not match the NNNN_name.sql convention"
            )
        version = int(match.group(1))
        if version in found:
            raise ValueError(f"duplicate migration version {version}: {path.name}")
        found[version] = Migration(
            version=version, name=match.group(2), sql=path.read_text(encoding="utf-8")
        )
    return [found[v] for v in sorted(found)]


async def migrate(pool: Any, *, directory: Path | str = MIGRATIONS_DIR) -> list[int]:
    """Apply every pending migration. Returns the versions applied."""
    migrations = discover(directory)
    applied: list[int] = []

    async with pool.acquire() as conn:
        # Serialize across replicas: two API pods booting together must not both
        # try to create the same table.
        await conn.execute("SELECT pg_advisory_lock($1)", _ADVISORY_LOCK_ID)
        try:
            await conn.execute(_BOOTSTRAP)
            rows = await conn.fetch("SELECT version FROM schema_migrations")
            done = {row["version"] for row in rows}

            for migration in migrations:
                if migration.version in done:
                    continue
                async with conn.transaction():
                    await conn.execute(migration.sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, name) VALUES ($1, $2)",
                        migration.version,
                        migration.name,
                    )
                applied.append(migration.version)
                log.info("migration.applied", version=migration.version, name=migration.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _ADVISORY_LOCK_ID)

    return applied
