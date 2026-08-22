ALTER TABLE tasks
    ADD COLUMN stage0_run_id UUID
        REFERENCES stage0_runs(stage0_run_id) ON DELETE RESTRICT,
    ADD CONSTRAINT manual_candidate_task_binding CHECK (
        workflow_type <> 'manual_candidate'
        OR (
            stage0_run_id IS NOT NULL
            AND target_id IS NOT NULL
            AND target_snapshot_id IS NOT NULL
            AND adapter_profile IS NOT NULL
            AND automatic_release_allowed = FALSE
        )
    );

ALTER TABLE baseline_epochs
    ADD COLUMN target_snapshot_id UUID
        REFERENCES target_snapshots(target_snapshot_id) ON DELETE RESTRICT,
    ADD COLUMN stage0_run_id UUID
        REFERENCES stage0_runs(stage0_run_id) ON DELETE RESTRICT,
    ADD COLUMN stage0_protocol_hash TEXT,
    ADD COLUMN source_snapshot_id UUID
        REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    ADD COLUMN workload_hash TEXT,
    ADD COLUMN image_digest TEXT,
    ADD COLUMN adapter_profile TEXT,
    ADD CONSTRAINT manual_candidate_baseline_binding CHECK (
        baseline_kind <> 'manual_candidate'
        OR (
            target_snapshot_id IS NOT NULL
            AND stage0_run_id IS NOT NULL
            AND stage0_protocol_hash IS NOT NULL
            AND source_snapshot_id IS NOT NULL
            AND workload_hash IS NOT NULL
            AND image_digest IS NOT NULL
            AND adapter_profile IS NOT NULL
        )
    );

ALTER TABLE candidates
    ADD COLUMN parent_candidate_id UUID
        REFERENCES candidates(candidate_id) ON DELETE RESTRICT,
    ADD COLUMN track TEXT,
    ADD COLUMN release_mode TEXT,
    ADD COLUMN candidate_kind TEXT,
    ADD COLUMN optimization_intent TEXT,
    ADD COLUMN replacement_point TEXT,
    ADD COLUMN verdict TEXT,
    ADD COLUMN evidence_bundle_id UUID
        REFERENCES evidence_bundles(evidence_id) ON DELETE RESTRICT,
    ADD COLUMN idempotency_key TEXT;

CREATE UNIQUE INDEX candidates_idempotency_idx
ON candidates (idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE TABLE manual_candidate_signoffs (
    signoff_id UUID PRIMARY KEY,
    task_id UUID NOT NULL UNIQUE REFERENCES tasks(task_id) ON DELETE CASCADE,
    candidate_id UUID NOT NULL UNIQUE REFERENCES candidates(candidate_id) ON DELETE RESTRICT,
    decision TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_bundle_id UUID NOT NULL
        REFERENCES evidence_bundles(evidence_id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT manual_candidate_signoff_decision_known
        CHECK (decision IN ('approved', 'rejected'))
);

CREATE INDEX manual_candidate_stage0_idx
ON tasks (stage0_run_id)
WHERE workflow_type = 'manual_candidate';

INSERT INTO schema_migrations (version, name)
VALUES (6, 'm1_manual_candidate_control_plane')
ON CONFLICT (version) DO NOTHING;
