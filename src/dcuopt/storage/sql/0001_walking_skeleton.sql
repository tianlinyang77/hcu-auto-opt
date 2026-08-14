CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    workload_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    project_mode TEXT,
    budget JSONB NOT NULL DEFAULT '{}'::jsonb,
    automatic_release_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mvp_never_auto_releases CHECK (automatic_release_allowed = FALSE)
);

CREATE TABLE IF NOT EXISTS stage0_evidence (
    task_id UUID PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
    evidence JSONB NOT NULL,
    report JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS baseline_epochs (
    baseline_epoch_id UUID PRIMARY KEY,
    task_id UUID NOT NULL UNIQUE REFERENCES tasks(task_id) ON DELETE RESTRICT,
    hardware_fingerprint TEXT NOT NULL,
    software_fingerprint TEXT NOT NULL,
    workload_id TEXT NOT NULL,
    configuration_hash TEXT NOT NULL,
    frozen BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT baseline_is_always_frozen CHECK (frozen = TRUE)
);

CREATE OR REPLACE FUNCTION reject_frozen_baseline_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'baseline epochs are immutable; create a new epoch';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS baseline_epochs_immutable ON baseline_epochs;
CREATE TRIGGER baseline_epochs_immutable
BEFORE UPDATE OR DELETE ON baseline_epochs
FOR EACH ROW EXECUTE FUNCTION reject_frozen_baseline_mutation();

CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    worker_type TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
    state TEXT NOT NULL DEFAULT 'online',
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS resources (
    resource_id TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'available',
    owner_job_id UUID,
    lease_id UUID,
    fencing_token BIGINT NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ,
    cleanup_evidence JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    job_type TEXT NOT NULL,
    accepted_worker_type TEXT NOT NULL,
    lease_scope TEXT NOT NULL DEFAULT 'none',
    payload JSONB NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    priority INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'queued',
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_by TEXT REFERENCES workers(worker_id) ON DELETE SET NULL,
    claim_token UUID,
    claimed_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    resource_id TEXT REFERENCES resources(resource_id) ON DELETE RESTRICT,
    fencing_token BIGINT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    result JSONB,
    last_error JSONB,
    workflow_advanced_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Upgrade the small queue table from the architecture-only bootstrap if it exists.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS task_id UUID REFERENCES tasks(task_id) ON DELETE CASCADE;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS accepted_worker_type TEXT NOT NULL DEFAULT 'agent';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS lease_scope TEXT NOT NULL DEFAULT 'none';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS resource_id TEXT REFERENCES resources(resource_id) ON DELETE RESTRICT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS fencing_token BIGINT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS result JSONB;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS last_error JSONB;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS workflow_advanced_at TIMESTAMPTZ;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency_idx
ON jobs (idempotency_key);

CREATE INDEX IF NOT EXISTS jobs_claim_idx
ON jobs (accepted_worker_type, state, available_at, priority DESC, created_at)
WHERE state = 'queued';

CREATE INDEX IF NOT EXISTS jobs_heartbeat_idx
ON jobs (state, heartbeat_at)
WHERE state = 'running';

CREATE TABLE IF NOT EXISTS hotspots (
    hotspot_id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    baseline_epoch_id UUID NOT NULL REFERENCES baseline_epochs(baseline_epoch_id),
    symbol TEXT NOT NULL,
    share_ratio DOUBLE PRECISION NOT NULL,
    opportunity_score DOUBLE PRECISION NOT NULL,
    patchability TEXT NOT NULL,
    evidence JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (task_id, symbol)
);

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    round_id UUID NOT NULL,
    baseline_epoch_id UUID NOT NULL REFERENCES baseline_epochs(baseline_epoch_id),
    source_hash TEXT NOT NULL,
    variant TEXT NOT NULL,
    state TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (task_id, round_id, ordinal),
    UNIQUE (task_id, source_hash)
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    candidate_id UUID NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    uri TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (candidate_id, content_hash)
);

CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    candidate_id UUID NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    passed BOOLEAN NOT NULL,
    protocol_version TEXT NOT NULL,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence_uri TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (candidate_id, phase)
);

CREATE TABLE IF NOT EXISTS job_events (
    event_id BIGSERIAL PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version, name)
VALUES (1, 'walking_skeleton')
ON CONFLICT (version) DO NOTHING;
