ALTER TABLE tasks
    ADD COLUMN workflow_type TEXT NOT NULL DEFAULT 'optimization',
    ADD COLUMN target_id TEXT,
    ADD COLUMN target_snapshot_id UUID,
    ADD COLUMN adapter_profile TEXT,
    ADD COLUMN retest_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE workers ADD COLUMN adapter_profile TEXT;
UPDATE workers
SET adapter_profile = capabilities ->> 'adapter_profile'
WHERE adapter_profile IS NULL;

ALTER TABLE jobs ADD COLUMN adapter_profile TEXT;

DROP INDEX IF EXISTS jobs_claim_idx;
CREATE INDEX jobs_claim_idx
ON jobs (
    accepted_worker_type,
    adapter_profile,
    state,
    available_at,
    priority DESC,
    created_at
)
WHERE state = 'queued';

CREATE TABLE target_snapshots (
    target_snapshot_id UUID PRIMARY KEY,
    target_id TEXT NOT NULL,
    target_fingerprint TEXT NOT NULL,
    specification JSONB NOT NULL,
    source_path TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT target_snapshots_fingerprint_key
        UNIQUE (target_id, target_fingerprint)
);

ALTER TABLE tasks
    ADD CONSTRAINT tasks_target_snapshot_fk
    FOREIGN KEY (target_snapshot_id)
    REFERENCES target_snapshots(target_snapshot_id) ON DELETE RESTRICT;

ALTER TABLE baseline_epochs
    ADD COLUMN baseline_kind TEXT NOT NULL DEFAULT 'optimization';

CREATE TABLE source_snapshots (
    snapshot_id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    candidate_id UUID REFERENCES candidates(candidate_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    repository TEXT NOT NULL,
    commit TEXT NOT NULL,
    tree_hash TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    worktree_uri TEXT NOT NULL,
    clean BOOLEAN NOT NULL,
    parent_snapshot_id UUID REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL UNIQUE,
    adapter_provenance JSONB NOT NULL,
    synthetic BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT source_snapshots_provenance_array
        CHECK (jsonb_typeof(adapter_provenance) = 'array'),
    CONSTRAINT source_snapshot_parent_kind
        CHECK (
            (kind = 'baseline' AND parent_snapshot_id IS NULL)
            OR (kind = 'candidate' AND parent_snapshot_id IS NOT NULL)
        )
);

ALTER TABLE artifacts
    ALTER COLUMN candidate_id DROP NOT NULL,
    ADD COLUMN source_snapshot_id UUID
        REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    ADD COLUMN build_recipe JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN sbom_uri TEXT,
    ADD COLUMN signature_uri TEXT,
    ADD COLUMN adapter_provenance JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN synthetic BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN idempotency_key TEXT;

CREATE UNIQUE INDEX artifacts_idempotency_idx
ON artifacts (idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE TABLE execution_requests (
    request_id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    candidate_id UUID REFERENCES candidates(candidate_id) ON DELETE CASCADE,
    evaluation_run_id UUID REFERENCES evaluation_runs(evaluation_run_id) ON DELETE CASCADE,
    target_id TEXT NOT NULL,
    request JSONB NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE evidence_bundles (
    evidence_id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    candidate_id UUID REFERENCES candidates(candidate_id) ON DELETE CASCADE,
    baseline_epoch_id UUID REFERENCES baseline_epochs(baseline_epoch_id) ON DELETE RESTRICT,
    evaluation_run_id UUID NOT NULL
        REFERENCES evaluation_runs(evaluation_run_id) ON DELETE CASCADE,
    target_id TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    artifact_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    measurement_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_uris JSONB NOT NULL DEFAULT '[]'::jsonb,
    adapter_provenance JSONB NOT NULL,
    synthetic BOOLEAN NOT NULL DEFAULT FALSE,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT evidence_artifact_ids_array CHECK (jsonb_typeof(artifact_ids) = 'array'),
    CONSTRAINT evidence_measurement_ids_array CHECK (jsonb_typeof(measurement_ids) = 'array'),
    CONSTRAINT evidence_raw_uris_array CHECK (jsonb_typeof(raw_uris) = 'array'),
    CONSTRAINT evidence_provenance_array CHECK (jsonb_typeof(adapter_provenance) = 'array')
);

CREATE TABLE task_events (
    event_id BIGSERIAL PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX task_events_task_idx ON task_events (task_id, created_at, event_id);
CREATE INDEX source_snapshots_task_idx ON source_snapshots (task_id, created_at);
CREATE INDEX execution_requests_task_idx ON execution_requests (task_id, created_at);
CREATE INDEX evidence_bundles_task_idx ON evidence_bundles (task_id, created_at);

INSERT INTO schema_migrations (version, name)
VALUES (3, 'framework_smoke_control_plane')
ON CONFLICT (version) DO NOTHING;
