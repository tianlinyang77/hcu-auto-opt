CREATE TABLE search_rounds (
    round_id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL UNIQUE,
    schema_version TEXT NOT NULL DEFAULT 'm2a-search-round-v1',
    state TEXT NOT NULL,
    run_mode TEXT NOT NULL,
    project_mode TEXT,
    target_snapshot_id UUID NOT NULL
        REFERENCES target_snapshots(target_snapshot_id) ON DELETE RESTRICT,
    stage0_run_id UUID NOT NULL
        REFERENCES stage0_runs(stage0_run_id) ON DELETE RESTRICT,
    stage0_protocol_hash TEXT NOT NULL,
    baseline_epoch_id UUID NOT NULL
        REFERENCES baseline_epochs(baseline_epoch_id) ON DELETE RESTRICT,
    hotspot_id UUID NOT NULL
        REFERENCES hotspots(hotspot_id) ON DELETE RESTRICT,
    replacement_point TEXT NOT NULL,
    workload_id TEXT NOT NULL,
    workload_hash TEXT NOT NULL,
    configuration_hash TEXT NOT NULL,
    image_digest TEXT NOT NULL,
    adapter_profile TEXT NOT NULL,
    declared_candidate_count INTEGER NOT NULL,
    max_promoted INTEGER NOT NULL,
    family_alpha DOUBLE PRECISION NOT NULL,
    search_plan_hash TEXT NOT NULL,
    holdout_plan_commitment TEXT NOT NULL,
    holdout_commitment_scheme TEXT NOT NULL DEFAULT 'sha256-nonce-v1',
    holdout_plan_authority_id TEXT NOT NULL,
    holdout_plan_authority_hash TEXT NOT NULL,
    holdout_plan_hash TEXT,
    holdout_reveal_lease_id UUID,
    holdout_reveal_evidence_hash TEXT,
    selection_rule_hash TEXT NOT NULL,
    budget JSONB NOT NULL,
    candidate_family_hash TEXT,
    artifact_family_hash TEXT,
    holdout_family_hash TEXT,
    automatic_release_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    intake_closed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT search_round_schema_known
        CHECK (schema_version = 'm2a-search-round-v1'),
    CONSTRAINT search_round_state_known CHECK (
        state IN (
            'intake_open', 'intake_closed', 'building', 'correctness',
            'search_measuring', 'search_barrier', 'holdout_measuring',
            'holdout_barrier', 'awaiting_signoff', 'scripted_completed',
            'completed', 'rejected', 'cancelled'
        )
    ),
    CONSTRAINT search_round_mode_known CHECK (run_mode IN ('scripted', 'formal')),
    CONSTRAINT search_round_holdout_scheme_known
        CHECK (holdout_commitment_scheme = 'sha256-nonce-v1'),
    CONSTRAINT search_round_project_mode_safe CHECK (
        (run_mode = 'scripted' AND project_mode IS NULL)
        OR (run_mode = 'formal' AND project_mode = 'degraded_manual_intake')
    ),
    CONSTRAINT search_round_terminal_mode_safe CHECK (
        (run_mode = 'scripted' AND state NOT IN ('awaiting_signoff', 'completed', 'rejected'))
        OR (run_mode = 'formal' AND state <> 'scripted_completed')
    ),
    CONSTRAINT search_round_candidate_count_valid
        CHECK (declared_candidate_count BETWEEN 2 AND 4),
    CONSTRAINT search_round_promotion_count_valid
        CHECK (max_promoted BETWEEN 1 AND 2 AND max_promoted <= declared_candidate_count),
    CONSTRAINT search_round_family_alpha_valid CHECK (family_alpha > 0 AND family_alpha < 1),
    CONSTRAINT search_round_budget_object CHECK (jsonb_typeof(budget) = 'object'),
    CONSTRAINT search_round_version_positive CHECK (version >= 1),
    CONSTRAINT search_round_never_auto_releases CHECK (automatic_release_allowed = FALSE),
    CONSTRAINT search_round_reveal_atomic CHECK (
        (holdout_plan_hash IS NULL
            AND holdout_reveal_lease_id IS NULL
            AND holdout_reveal_evidence_hash IS NULL)
        OR (holdout_plan_hash IS NOT NULL
            AND holdout_reveal_lease_id IS NOT NULL
            AND holdout_reveal_evidence_hash IS NOT NULL
            AND holdout_family_hash IS NOT NULL)
    ),
    CONSTRAINT search_round_protocol_hash_valid
        CHECK (stage0_protocol_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT search_round_workload_hash_valid
        CHECK (workload_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT search_round_configuration_hash_valid
        CHECK (configuration_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT search_round_image_digest_valid
        CHECK (image_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT search_round_search_plan_hash_valid
        CHECK (search_plan_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT search_round_holdout_commitment_valid
        CHECK (holdout_plan_commitment ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT search_round_holdout_authority_hash_valid
        CHECK (holdout_plan_authority_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT search_round_selection_rule_hash_valid
        CHECK (selection_rule_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT search_round_candidate_family_hash_valid CHECK (
        candidate_family_hash IS NULL
        OR candidate_family_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT search_round_artifact_family_hash_valid CHECK (
        artifact_family_hash IS NULL
        OR artifact_family_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT search_round_holdout_family_hash_valid CHECK (
        holdout_family_hash IS NULL
        OR holdout_family_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT search_round_holdout_plan_hash_valid CHECK (
        holdout_plan_hash IS NULL
        OR holdout_plan_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT search_round_reveal_evidence_hash_valid CHECK (
        holdout_reveal_evidence_hash IS NULL
        OR holdout_reveal_evidence_hash ~ '^sha256:[0-9a-f]{64}$'
    )
);

CREATE UNIQUE INDEX search_rounds_one_active_per_task_idx
ON search_rounds (task_id)
WHERE state NOT IN ('scripted_completed', 'completed', 'rejected', 'cancelled');

CREATE INDEX search_rounds_state_idx
ON search_rounds (state, created_at, round_id);

CREATE TABLE round_candidates (
    round_candidate_id UUID PRIMARY KEY,
    round_id UUID NOT NULL REFERENCES search_rounds(round_id) ON DELETE CASCADE,
    candidate_id UUID NOT NULL REFERENCES candidates(candidate_id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL,
    source_package_store_id TEXT NOT NULL,
    source_package_store_hash TEXT NOT NULL,
    source_package_hash TEXT NOT NULL,
    source_manifest_version TEXT NOT NULL DEFAULT 'm1-candidate-source-v1',
    source_manifest_hash TEXT NOT NULL,
    baseline_source_hash TEXT NOT NULL,
    candidate_source_hash TEXT NOT NULL,
    optimization_intent TEXT NOT NULL,
    replacement_point TEXT NOT NULL,
    track TEXT NOT NULL DEFAULT 'triton',
    release_mode TEXT NOT NULL DEFAULT 'overlay',
    candidate_kind TEXT NOT NULL,
    artifact_id UUID REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    artifact_hash TEXT,
    terminal_failure_code TEXT,
    failure_evidence_hash TEXT,
    state TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT round_candidate_ordinal_valid CHECK (ordinal BETWEEN 0 AND 3),
    CONSTRAINT round_candidate_manifest_version_known
        CHECK (source_manifest_version = 'm1-candidate-source-v1'),
    CONSTRAINT round_candidate_track_known CHECK (track = 'triton'),
    CONSTRAINT round_candidate_release_mode_known CHECK (release_mode = 'overlay'),
    CONSTRAINT round_candidate_kind_known CHECK (candidate_kind IN ('fixture', 'business')),
    CONSTRAINT round_candidate_state_known CHECK (
        state IN (
            'intake_accepted', 'building', 'build_failed', 'built',
            'correctness_failed', 'correctness_passed', 'search_failed',
            'search_measured', 'not_promoted', 'holdout_failed',
            'holdout_measured', 'invalid'
        )
    ),
    CONSTRAINT round_candidate_source_differs
        CHECK (baseline_source_hash <> candidate_source_hash),
    CONSTRAINT round_candidate_artifact_pair CHECK (
        (artifact_id IS NULL AND artifact_hash IS NULL)
        OR (artifact_id IS NOT NULL AND artifact_hash IS NOT NULL)
    ),
    CONSTRAINT round_candidate_failure_pair CHECK (
        (terminal_failure_code IS NULL AND failure_evidence_hash IS NULL)
        OR (terminal_failure_code IS NOT NULL AND failure_evidence_hash IS NOT NULL)
    ),
    CONSTRAINT round_candidate_terminal_unambiguous CHECK (
        artifact_id IS NULL OR terminal_failure_code IS NULL
    ),
    CONSTRAINT round_candidate_store_hash_valid
        CHECK (source_package_store_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT round_candidate_package_hash_valid
        CHECK (source_package_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT round_candidate_manifest_hash_valid
        CHECK (source_manifest_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT round_candidate_baseline_hash_valid
        CHECK (baseline_source_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT round_candidate_source_hash_valid
        CHECK (candidate_source_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT round_candidate_artifact_hash_valid CHECK (
        artifact_hash IS NULL OR artifact_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT round_candidate_failure_evidence_hash_valid CHECK (
        failure_evidence_hash IS NULL
        OR failure_evidence_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    UNIQUE (round_id, candidate_id),
    UNIQUE (round_id, round_candidate_id),
    UNIQUE (round_id, ordinal),
    UNIQUE (round_id, candidate_source_hash),
    UNIQUE (round_id, source_package_hash),
    UNIQUE (round_id, source_manifest_hash)
);

CREATE INDEX round_candidates_round_state_idx
ON round_candidates (round_id, state, ordinal);

CREATE TABLE round_budget_reservations (
    reservation_id UUID PRIMARY KEY,
    round_id UUID NOT NULL REFERENCES search_rounds(round_id) ON DELETE CASCADE,
    job_id UUID NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    attempt INTEGER NOT NULL,
    candidate_id UUID REFERENCES candidates(candidate_id) ON DELETE RESTRICT,
    phase TEXT,
    planned JSONB NOT NULL,
    state TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT round_budget_attempt_positive CHECK (attempt >= 1),
    CONSTRAINT round_budget_phase_known CHECK (phase IS NULL OR phase IN ('search', 'holdout')),
    CONSTRAINT round_budget_reservation_state_known
        CHECK (state IN ('reserved', 'settled', 'released')),
    CONSTRAINT round_budget_planned_object CHECK (jsonb_typeof(planned) = 'object'),
    UNIQUE (job_id, attempt),
    UNIQUE (reservation_id, round_id)
);

CREATE INDEX round_budget_reservations_round_state_idx
ON round_budget_reservations (round_id, state, created_at);

CREATE TABLE round_budget_ledger (
    ledger_entry_id UUID PRIMARY KEY,
    reservation_id UUID NOT NULL,
    round_id UUID NOT NULL,
    entry_type TEXT NOT NULL,
    reserved JSONB NOT NULL,
    actual JSONB NOT NULL,
    lease_held_seconds DOUBLE PRECISION NOT NULL,
    harness_active_seconds DOUBLE PRECISION NOT NULL,
    raw_usage_evidence_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT round_budget_ledger_reservation_fk
        FOREIGN KEY (reservation_id, round_id)
        REFERENCES round_budget_reservations(reservation_id, round_id) ON DELETE RESTRICT,
    CONSTRAINT round_budget_entry_type_known
        CHECK (entry_type IN ('reserve', 'settle', 'release')),
    CONSTRAINT round_budget_reserved_object CHECK (jsonb_typeof(reserved) = 'object'),
    CONSTRAINT round_budget_actual_object CHECK (jsonb_typeof(actual) = 'object'),
    CONSTRAINT round_budget_runtime_nonnegative
        CHECK (lease_held_seconds >= 0 AND harness_active_seconds >= 0),
    CONSTRAINT round_budget_harness_within_lease
        CHECK (harness_active_seconds <= lease_held_seconds),
    CONSTRAINT round_budget_nonsettle_runtime_zero CHECK (
        entry_type = 'settle'
        OR (lease_held_seconds = 0 AND harness_active_seconds = 0)
    ),
    CONSTRAINT round_budget_usage_hash_valid
        CHECK (raw_usage_evidence_hash ~ '^sha256:[0-9a-f]{64}$'),
    UNIQUE (reservation_id, entry_type),
    UNIQUE (round_id, idempotency_key)
);

CREATE UNIQUE INDEX round_budget_ledger_terminal_idx
ON round_budget_ledger (reservation_id)
WHERE entry_type IN ('settle', 'release');

CREATE INDEX round_budget_ledger_round_idx
ON round_budget_ledger (round_id, created_at, ledger_entry_id);

CREATE OR REPLACE FUNCTION reject_round_budget_ledger_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'round budget ledger is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER round_budget_ledger_append_only
BEFORE UPDATE OR DELETE ON round_budget_ledger
FOR EACH ROW EXECUTE FUNCTION reject_round_budget_ledger_mutation();

CREATE OR REPLACE FUNCTION enforce_round_budget_reservation_transition()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.state IN ('settled', 'released') THEN
        RAISE EXCEPTION 'terminal round budget reservation is immutable';
    END IF;
    IF OLD.state = 'reserved' AND NEW.state NOT IN ('reserved', 'settled', 'released') THEN
        RAISE EXCEPTION 'invalid round budget reservation transition';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER round_budget_reservation_transition
BEFORE UPDATE ON round_budget_reservations
FOR EACH ROW EXECUTE FUNCTION enforce_round_budget_reservation_transition();

INSERT INTO schema_migrations (version, name)
VALUES (8, 'm2_search_round')
ON CONFLICT (version) DO NOTHING;
