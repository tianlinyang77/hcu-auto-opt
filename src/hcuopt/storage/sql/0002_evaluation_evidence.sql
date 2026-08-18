ALTER TABLE evaluations RENAME TO evaluation_runs;
ALTER TABLE evaluation_runs RENAME COLUMN evaluation_id TO evaluation_run_id;

ALTER TABLE evaluation_runs
    DROP CONSTRAINT IF EXISTS evaluations_candidate_id_phase_key;

ALTER TABLE evaluation_runs
    ADD COLUMN round_id UUID,
    ADD COLUMN baseline_epoch_id UUID REFERENCES baseline_epochs(baseline_epoch_id),
    ADD COLUMN target_fingerprint TEXT,
    ADD COLUMN idempotency_key TEXT,
    ADD COLUMN measurement JSONB,
    ADD COLUMN evidence_uris JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN adapter_provenance JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN synthetic BOOLEAN NOT NULL DEFAULT TRUE;

UPDATE evaluation_runs AS run
SET round_id = candidate.round_id,
    baseline_epoch_id = candidate.baseline_epoch_id,
    target_fingerprint = concat_ws(
        ':',
        baseline.hardware_fingerprint,
        baseline.software_fingerprint,
        baseline.configuration_hash
    ),
    idempotency_key = 'legacy:' || run.evaluation_run_id::text,
    evidence_uris = CASE
        WHEN run.evidence_uri IS NULL THEN '[]'::jsonb
        ELSE to_jsonb(ARRAY[run.evidence_uri])
    END,
    adapter_provenance = jsonb_build_array(
        jsonb_build_object(
            'profile', 'fake-v1-control-flow-only',
            'capability', 'legacy_evaluation',
            'adapter_name', 'LegacyWalkingSkeleton',
            'adapter_version', '1',
            'implementation_kind', 'fake',
            'source_commit', NULL
        )
    )
FROM candidates AS candidate
JOIN baseline_epochs AS baseline
  ON baseline.baseline_epoch_id = candidate.baseline_epoch_id
WHERE run.candidate_id = candidate.candidate_id;

UPDATE evaluation_runs
SET passed = NULL,
    metrics = (
        metrics
        - 'speedup_ratio'
        - 'e2e_speedup_ratio'
        - 'latency'
        - 'throughput'
        - 'ci_low'
        - 'ci_high'
        - 'passed'
        - 'synthetic'
    ) || jsonb_build_object(
        'measurement_status', 'not_measured',
        'migration_note', 'legacy synthetic performance claims removed by F0.5'
    )
WHERE synthetic = TRUE AND phase IN ('performance', 'e2e');

ALTER TABLE evaluation_runs
    ALTER COLUMN passed DROP NOT NULL,
    ALTER COLUMN round_id SET NOT NULL,
    ALTER COLUMN baseline_epoch_id SET NOT NULL,
    ALTER COLUMN target_fingerprint SET NOT NULL,
    ALTER COLUMN idempotency_key SET NOT NULL,
    ADD CONSTRAINT evaluation_runs_idempotency_key_key UNIQUE (idempotency_key),
    ADD CONSTRAINT evaluation_runs_evidence_uris_array
        CHECK (jsonb_typeof(evidence_uris) = 'array'),
    ADD CONSTRAINT evaluation_runs_adapter_provenance_array
        CHECK (jsonb_typeof(adapter_provenance) = 'array');

CREATE INDEX evaluation_runs_candidate_phase_idx
ON evaluation_runs (candidate_id, phase, created_at DESC);

CREATE INDEX evaluation_runs_task_idx
ON evaluation_runs (task_id, created_at);

CREATE TABLE execution_attempts (
    execution_attempt_id UUID PRIMARY KEY,
    evaluation_run_id UUID NOT NULL
        REFERENCES evaluation_runs(evaluation_run_id) ON DELETE CASCADE,
    request_id UUID NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    status TEXT NOT NULL,
    exit_code INTEGER,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    stdout_uri TEXT,
    stderr_uri TEXT,
    result_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    adapter_provenance JSONB NOT NULL,
    synthetic BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT execution_attempts_status_known
        CHECK (status IN ('succeeded', 'failed', 'timed_out', 'cancelled')),
    CONSTRAINT execution_attempts_finished_after_started
        CHECK (finished_at >= started_at),
    CONSTRAINT execution_attempts_success_exit_zero
        CHECK (status <> 'succeeded' OR (exit_code IS NOT NULL AND exit_code = 0)),
    CONSTRAINT execution_attempts_adapter_provenance_object
        CHECK (jsonb_typeof(adapter_provenance) = 'object'),
    CONSTRAINT execution_attempts_run_attempt_key
        UNIQUE (evaluation_run_id, attempt_number)
);

CREATE INDEX execution_attempts_request_idx
ON execution_attempts (request_id, attempt_number);

INSERT INTO schema_migrations (version, name)
VALUES (2, 'evaluation_evidence')
ON CONFLICT (version) DO NOTHING;
