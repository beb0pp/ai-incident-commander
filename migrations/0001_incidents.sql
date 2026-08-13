-- Incidents are stored as one JSONB document per aggregate. The scalar columns
-- are projections of that document, kept only so the operational filters stay
-- index-backed; the document remains the source of truth.

CREATE TABLE IF NOT EXISTS incidents (
    id          TEXT PRIMARY KEY,
    status      TEXT        NOT NULL,
    severity    TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL,
    document    JSONB       NOT NULL
);

CREATE INDEX IF NOT EXISTS incidents_created_at_idx
    ON incidents (created_at DESC);

-- Partial index: the dashboard query is always "what is still open", and the
-- resolved rows are the ones that accumulate forever.
CREATE INDEX IF NOT EXISTS incidents_open_idx
    ON incidents (status, updated_at DESC)
    WHERE status <> 'resolved';

CREATE INDEX IF NOT EXISTS incidents_severity_idx
    ON incidents (severity, created_at DESC);
