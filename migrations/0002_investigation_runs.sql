-- Durable record of every investigation, kept separately from the incident so a
-- re-investigation never overwrites the previous run's evidence. This is the
-- audit trail: what each agent saw, what it concluded, and what it cost.

CREATE TABLE IF NOT EXISTS investigation_runs (
    run_id        TEXT PRIMARY KEY,
    incident_id   TEXT        NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    started_at    TIMESTAMPTZ NOT NULL,
    finished_at   TIMESTAMPTZ,
    failed_nodes  TEXT[]      NOT NULL DEFAULT '{}',
    input_tokens  INTEGER     NOT NULL DEFAULT 0,
    output_tokens INTEGER     NOT NULL DEFAULT 0,
    state         JSONB       NOT NULL
);

CREATE INDEX IF NOT EXISTS investigation_runs_incident_idx
    ON investigation_runs (incident_id, started_at DESC);

-- Approval decisions are append-only and queried by auditors independently of
-- the incident document they are also embedded in.
CREATE TABLE IF NOT EXISTS approval_decisions (
    id          BIGSERIAL PRIMARY KEY,
    incident_id TEXT        NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    action_id   TEXT        NOT NULL,
    approved    BOOLEAN     NOT NULL,
    decided_by  TEXT        NOT NULL,
    decided_at  TIMESTAMPTZ NOT NULL,
    comment     TEXT
);

CREATE INDEX IF NOT EXISTS approval_decisions_incident_idx
    ON approval_decisions (incident_id, decided_at DESC);
