ALTER TABLE jobs ADD COLUMN lease_id UUID;

ALTER TABLE tasks
    ADD COLUMN stage0_authority TEXT NOT NULL DEFAULT 'none',
    ADD CONSTRAINT tasks_stage0_authority_known
        CHECK (stage0_authority IN ('none', 'synthetic', 'formal'));

CREATE TABLE framework_smoke_signoffs (
    signoff_id UUID PRIMARY KEY,
    task_id UUID NOT NULL UNIQUE REFERENCES tasks(task_id) ON DELETE CASCADE,
    decision TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_bundle_id UUID NOT NULL
        REFERENCES evidence_bundles(evidence_id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT framework_smoke_signoff_decision_known
        CHECK (decision IN ('approved', 'rejected'))
);

CREATE TABLE stage0_runs (
    stage0_run_id UUID PRIMARY KEY,
    task_id UUID NOT NULL UNIQUE REFERENCES tasks(task_id) ON DELETE CASCADE,
    target_snapshot_id UUID NOT NULL
        REFERENCES target_snapshots(target_snapshot_id) ON DELETE RESTRICT,
    adapter_profile TEXT NOT NULL,
    mode TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'collecting',
    protocol_version TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    report JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalized_at TIMESTAMPTZ,
    CONSTRAINT stage0_run_mode_known CHECK (mode IN ('dry_run', 'formal')),
    CONSTRAINT stage0_run_state_known
        CHECK (state IN ('collecting', 'ready', 'finalized', 'failed'))
);

CREATE TABLE stage0_probe_records (
    probe_record_id UUID PRIMARY KEY,
    stage0_run_id UUID NOT NULL REFERENCES stage0_runs(stage0_run_id) ON DELETE CASCADE,
    job_id UUID NOT NULL UNIQUE REFERENCES jobs(job_id) ON DELETE RESTRICT,
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    target_snapshot_id UUID NOT NULL
        REFERENCES target_snapshots(target_snapshot_id) ON DELETE RESTRICT,
    probe_type TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    raw_evidence_uri TEXT,
    raw_evidence_hash TEXT,
    summary JSONB NOT NULL,
    adapter_provenance JSONB NOT NULL,
    synthetic BOOLEAN NOT NULL,
    lease_id UUID,
    resource_id TEXT,
    fencing_token BIGINT,
    cleanup_evidence JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT stage0_probe_run_type_key UNIQUE (stage0_run_id, probe_type),
    CONSTRAINT stage0_probe_type_known CHECK (
        probe_type IN (
            'fingerprint', 'timer', 'noise', 'known_signal',
            'null_signal', 'profiler', 'hotpatch'
        )
    ),
    CONSTRAINT stage0_probe_provenance_array
        CHECK (jsonb_typeof(adapter_provenance) = 'array')
);

ALTER TABLE stage0_evidence
    ADD COLUMN stage0_run_id UUID UNIQUE
        REFERENCES stage0_runs(stage0_run_id) ON DELETE RESTRICT;

CREATE INDEX stage0_probe_records_run_idx
ON stage0_probe_records (stage0_run_id, created_at);

INSERT INTO schema_migrations (version, name)
VALUES (5, 'stage0_control_plane')
ON CONFLICT (version) DO NOTHING;
