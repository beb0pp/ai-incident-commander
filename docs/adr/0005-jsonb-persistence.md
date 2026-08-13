# ADR 0005 — JSONB documents and SQL migrations, not an ORM

**Status:** Accepted · **Date:** 2026-08-13

## Context

Incidents need to persist: the incident itself, its plan, and the approval
decisions taken against it. The reflexive stack is SQLAlchemy plus Alembic.

Two facts about this data shaped the decision. The incident aggregate is read and
written **whole** — there is no query that wants half an incident, and no
cross-aggregate join. And its shape is still moving: the plan structure and the
evidence model are the parts of this project most likely to change.

## Decision

**Storage:** one JSONB document per aggregate, with scalar columns (`status`,
`severity`, `created_at`, `updated_at`) projected out of it purely so the
operational filters stay index-backed. The document is the source of truth; the
columns are a materialized projection.

**Access:** asyncpg with explicit SQL, behind an `IncidentRepository` protocol.
Two implementations: in-memory for tests and the offline demo, Postgres for
production.

**Migrations:** numbered `.sql` files in `migrations/`, applied by a ~60-line
runner that takes a Postgres advisory lock (so two API replicas booting together
cannot race), applies each pending file in a transaction, and records it in
`schema_migrations`.

The reasoning for each: an ORM earns its cost through relational mapping and a
query layer, and this schema has neither. Alembic earns its cost through
autogeneration and branching history against a moving relational schema — with
two JSONB tables it is machinery without a job. Pydantic already does the
serialization an ORM would be doing.

## Consequences

**Accepted:** no ad-hoc relational querying. "Every incident that touched
service X" means a JSONB path query, and a future analytics need would mean
normalizing.

**Accepted:** migrations are forward-only, with no down-migrations. That is a
deliberate constraint rather than a missing feature — rolling a schema backwards
in production is usually worse than rolling forward.

**Accepted:** schema evolution is our problem. An old document must stay readable
by new code, which means new fields carry defaults. Pydantic makes that explicit
at the model, which is the right place for it.

**Gained:** the persistence layer is small enough to read in one sitting, and the
SQL that runs is the SQL in the file.

**Gained:** the in-memory repository is a complete implementation of the same
protocol, not a mock — so the tests exercise real code paths.

## Revisit if

Cross-aggregate queries appear, or a second consumer needs relational access to
this data. Both mean normalizing, and at that point SQLAlchemy plus Alembic is
the right call.
