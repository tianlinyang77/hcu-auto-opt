"""PostgreSQL queue primitives.

These statements intentionally depend on PostgreSQL semantics. They must be
tested against PostgreSQL, never silently substituted with SQLite.
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id UUID PRIMARY KEY,
    job_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'queued',
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_by TEXT,
    claim_token UUID,
    claimed_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jobs_claim_idx
ON jobs (state, available_at, priority DESC, created_at)
WHERE state = 'queued';
"""


def claim_jobs_sql(limit: int = 1) -> str:
    if limit < 1:
        raise ValueError("limit must be positive")
    return f"""
WITH selected AS (
    SELECT job_id
    FROM jobs
    WHERE state = 'queued' AND available_at <= now()
    ORDER BY priority DESC, created_at
    FOR UPDATE SKIP LOCKED
    LIMIT {limit}
)
UPDATE jobs AS j
SET state = 'running',
    claimed_by = %(worker_id)s,
    claim_token = %(claim_token)s,
    claimed_at = now(),
    heartbeat_at = now(),
    attempts = attempts + 1
FROM selected
WHERE j.job_id = selected.job_id
RETURNING j.*;
"""

