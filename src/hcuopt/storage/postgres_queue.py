"""PostgreSQL queue primitives.

These statements intentionally depend on PostgreSQL semantics. They must be
tested against PostgreSQL, never silently substituted with SQLite.
"""

from hcuopt.storage.migrations import migration_sql

POSTGRES_SCHEMA = migration_sql()


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


def claim_for_worker_sql() -> str:
    return """
WITH selected AS (
    SELECT job_id
    FROM jobs
    WHERE state = 'queued'
      AND available_at <= now()
      AND accepted_worker_type = %(worker_type)s
    ORDER BY priority DESC, created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE jobs AS j
SET state = 'running',
    claimed_by = %(worker_id)s,
    claim_token = %(claim_token)s,
    claimed_at = now(),
    heartbeat_at = now(),
    attempts = attempts + 1,
    updated_at = now()
FROM selected
WHERE j.job_id = selected.job_id
RETURNING j.*;
"""
