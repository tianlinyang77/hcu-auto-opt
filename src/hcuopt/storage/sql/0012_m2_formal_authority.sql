CREATE TABLE formal_round_authority_contexts (
    authority_context_id UUID PRIMARY KEY,
    context_hash TEXT NOT NULL UNIQUE,
    round_id UUID NOT NULL UNIQUE REFERENCES search_rounds(round_id) ON DELETE RESTRICT,
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    target_snapshot_id UUID NOT NULL
        REFERENCES target_snapshots(target_snapshot_id) ON DELETE RESTRICT,
    stage0_run_id UUID NOT NULL REFERENCES stage0_runs(stage0_run_id) ON DELETE RESTRICT,
    stage0_protocol_hash TEXT NOT NULL,
    baseline_epoch_id UUID NOT NULL
        REFERENCES baseline_epochs(baseline_epoch_id) ON DELETE RESTRICT,
    hotspot_id UUID NOT NULL REFERENCES hotspots(hotspot_id) ON DELETE RESTRICT,
    target_profile_hash TEXT NOT NULL,
    workload_profile_hash TEXT NOT NULL,
    measurement_profile_hash TEXT NOT NULL,
    candidate_family_hash TEXT NOT NULL,
    artifact_family_hash TEXT NOT NULL,
    search_plan_hash TEXT NOT NULL,
    holdout_plan_commitment TEXT NOT NULL,
    holdout_plan_authority_id TEXT NOT NULL,
    holdout_plan_authority_hash TEXT NOT NULL,
    selection_rule_hash TEXT NOT NULL,
    evidence_store_id TEXT NOT NULL,
    evidence_store_version INTEGER NOT NULL,
    evidence_store_hash TEXT NOT NULL,
    evidence_access_policy_hash TEXT NOT NULL,
    verifier_id TEXT NOT NULL,
    verifier_version TEXT NOT NULL,
    verifier_hash TEXT NOT NULL,
    sealed_by TEXT NOT NULL,
    sealed_at TIMESTAMPTZ NOT NULL,
    run_mode TEXT NOT NULL DEFAULT 'formal',
    project_mode TEXT NOT NULL DEFAULT 'degraded_manual_intake',
    synthetic BOOLEAN NOT NULL DEFAULT FALSE,
    automatic_release_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT formal_authority_run_mode CHECK (run_mode = 'formal'),
    CONSTRAINT formal_authority_project_mode
        CHECK (project_mode = 'degraded_manual_intake'),
    CONSTRAINT formal_authority_non_synthetic CHECK (synthetic = FALSE),
    CONSTRAINT formal_authority_never_auto_releases
        CHECK (automatic_release_allowed = FALSE),
    CONSTRAINT formal_authority_store_version_positive
        CHECK (evidence_store_version >= 1),
    CONSTRAINT formal_authority_context_hash_valid
        CHECK (context_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT formal_authority_all_hashes_valid CHECK (
        stage0_protocol_hash ~ '^sha256:[0-9a-f]{64}$'
        AND target_profile_hash ~ '^sha256:[0-9a-f]{64}$'
        AND workload_profile_hash ~ '^sha256:[0-9a-f]{64}$'
        AND measurement_profile_hash ~ '^sha256:[0-9a-f]{64}$'
        AND candidate_family_hash ~ '^sha256:[0-9a-f]{64}$'
        AND artifact_family_hash ~ '^sha256:[0-9a-f]{64}$'
        AND search_plan_hash ~ '^sha256:[0-9a-f]{64}$'
        AND holdout_plan_commitment ~ '^sha256:[0-9a-f]{64}$'
        AND holdout_plan_authority_hash ~ '^sha256:[0-9a-f]{64}$'
        AND selection_rule_hash ~ '^sha256:[0-9a-f]{64}$'
        AND evidence_store_hash ~ '^sha256:[0-9a-f]{64}$'
        AND evidence_access_policy_hash ~ '^sha256:[0-9a-f]{64}$'
        AND verifier_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    UNIQUE (round_id, authority_context_id, context_hash),
    UNIQUE (
        round_id, authority_context_id, context_hash,
        evidence_store_id, evidence_store_hash
    ),
    UNIQUE (
        round_id, authority_context_id, context_hash, task_id,
        evidence_store_id, evidence_store_hash
    )
);

CREATE OR REPLACE FUNCTION enforce_formal_authority_context_parent()
RETURNS TRIGGER AS $$
DECLARE
    parent search_rounds%ROWTYPE;
    current_task tasks%ROWTYPE;
    stage0 stage0_runs%ROWTYPE;
    stage0_task tasks%ROWTYPE;
    stage0_record stage0_evidence%ROWTYPE;
    baseline baseline_epochs%ROWTYPE;
    baseline_task tasks%ROWTYPE;
    baseline_source source_snapshots%ROWTYPE;
    hotspot hotspots%ROWTYPE;
    snapshot target_snapshots%ROWTYPE;
BEGIN
    SELECT * INTO parent FROM search_rounds WHERE round_id = NEW.round_id FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Formal Authority parent Round does not exist';
    END IF;
    SELECT * INTO current_task FROM tasks WHERE task_id = NEW.task_id FOR SHARE;
    SELECT * INTO stage0 FROM stage0_runs
    WHERE stage0_run_id = NEW.stage0_run_id FOR SHARE;
    SELECT * INTO stage0_task FROM tasks WHERE task_id = stage0.task_id FOR SHARE;
    SELECT * INTO stage0_record FROM stage0_evidence
    WHERE stage0_run_id = NEW.stage0_run_id FOR SHARE;
    SELECT * INTO baseline FROM baseline_epochs
    WHERE baseline_epoch_id = NEW.baseline_epoch_id FOR SHARE;
    SELECT * INTO baseline_task FROM tasks WHERE task_id = baseline.task_id FOR SHARE;
    SELECT * INTO baseline_source FROM source_snapshots
    WHERE snapshot_id = baseline.source_snapshot_id FOR SHARE;
    SELECT * INTO hotspot FROM hotspots WHERE hotspot_id = NEW.hotspot_id FOR SHARE;
    SELECT * INTO snapshot FROM target_snapshots
    WHERE target_snapshot_id = NEW.target_snapshot_id FOR SHARE;

    IF current_task.task_id IS NULL
        OR stage0.stage0_run_id IS NULL
        OR stage0_task.task_id IS NULL
        OR stage0_record.stage0_run_id IS NULL
        OR baseline.baseline_epoch_id IS NULL
        OR baseline_task.task_id IS NULL
        OR baseline_source.snapshot_id IS NULL
        OR hotspot.hotspot_id IS NULL
        OR snapshot.target_snapshot_id IS NULL
        OR parent.run_mode IS DISTINCT FROM 'formal'
        OR parent.project_mode IS DISTINCT FROM 'degraded_manual_intake'
        OR parent.state NOT IN ('correctness', 'search_measuring')
        OR parent.task_id IS DISTINCT FROM NEW.task_id
        OR parent.target_snapshot_id IS DISTINCT FROM NEW.target_snapshot_id
        OR parent.stage0_run_id IS DISTINCT FROM NEW.stage0_run_id
        OR parent.stage0_protocol_hash IS DISTINCT FROM NEW.stage0_protocol_hash
        OR parent.baseline_epoch_id IS DISTINCT FROM NEW.baseline_epoch_id
        OR parent.hotspot_id IS DISTINCT FROM NEW.hotspot_id
        OR parent.candidate_family_hash IS NULL
        OR parent.candidate_family_hash IS DISTINCT FROM NEW.candidate_family_hash
        OR parent.artifact_family_hash IS NULL
        OR parent.artifact_family_hash IS DISTINCT FROM NEW.artifact_family_hash
        OR parent.search_plan_hash IS DISTINCT FROM NEW.search_plan_hash
        OR parent.holdout_plan_commitment IS DISTINCT FROM NEW.holdout_plan_commitment
        OR parent.holdout_plan_authority_id IS DISTINCT FROM NEW.holdout_plan_authority_id
        OR parent.holdout_plan_authority_hash IS DISTINCT FROM NEW.holdout_plan_authority_hash
        OR parent.selection_rule_hash IS DISTINCT FROM NEW.selection_rule_hash
        OR parent.automatic_release_allowed
        OR current_task.workflow_type IS DISTINCT FROM 'search_round'
        OR current_task.stage0_authority IS DISTINCT FROM 'formal'
        OR current_task.project_mode IS DISTINCT FROM 'degraded_manual_intake'
        OR current_task.target_snapshot_id IS DISTINCT FROM NEW.target_snapshot_id
        OR current_task.stage0_run_id IS DISTINCT FROM NEW.stage0_run_id
        OR current_task.adapter_profile IS DISTINCT FROM parent.adapter_profile
        OR current_task.workload_id IS DISTINCT FROM parent.workload_id
        OR current_task.target_id IS DISTINCT FROM snapshot.target_id
        OR current_task.automatic_release_allowed
        OR stage0.mode IS DISTINCT FROM 'formal'
        OR stage0.state IS DISTINCT FROM 'finalized'
        OR stage0.target_snapshot_id IS DISTINCT FROM NEW.target_snapshot_id
        OR stage0_task.stage0_authority IS DISTINCT FROM 'formal'
        OR stage0_task.project_mode IS DISTINCT FROM 'degraded_manual_intake'
        OR stage0_task.target_snapshot_id IS DISTINCT FROM NEW.target_snapshot_id
        OR stage0_task.automatic_release_allowed
        OR stage0_record.evidence -> 'synthetic' IS DISTINCT FROM 'false'::jsonb
        OR stage0_record.evidence ->> 'stage0_run_id'
            IS DISTINCT FROM NEW.stage0_run_id::TEXT
        OR stage0_record.evidence ->> 'protocol_version'
            IS DISTINCT FROM stage0.protocol_version
        OR stage0_record.evidence ->> 'protocol_hash'
            IS DISTINCT FROM NEW.stage0_protocol_hash
        OR stage0_record.report ->> 'evidence_authority' IS DISTINCT FROM 'formal'
        OR stage0_record.report -> 'automatic_release_allowed'
            IS DISTINCT FROM 'false'::jsonb
        OR baseline.target_snapshot_id IS DISTINCT FROM NEW.target_snapshot_id
        OR baseline.stage0_run_id IS DISTINCT FROM NEW.stage0_run_id
        OR baseline.stage0_protocol_hash IS DISTINCT FROM NEW.stage0_protocol_hash
        OR baseline.workload_id IS DISTINCT FROM parent.workload_id
        OR baseline.workload_hash IS DISTINCT FROM parent.workload_hash
        OR baseline.configuration_hash IS DISTINCT FROM parent.configuration_hash
        OR baseline.image_digest IS DISTINCT FROM parent.image_digest
        OR baseline.adapter_profile IS DISTINCT FROM parent.adapter_profile
        OR baseline_task.stage0_authority IS DISTINCT FROM 'formal'
        OR baseline_task.target_snapshot_id IS DISTINCT FROM NEW.target_snapshot_id
        OR baseline_task.stage0_run_id IS DISTINCT FROM NEW.stage0_run_id
        OR baseline_task.automatic_release_allowed
        OR baseline_source.kind IS DISTINCT FROM 'baseline'
        OR NOT baseline_source.clean
        OR baseline_source.synthetic
        OR hotspot.task_id IS DISTINCT FROM baseline.task_id
        OR hotspot.baseline_epoch_id IS DISTINCT FROM NEW.baseline_epoch_id
        OR hotspot.candidate_kind IS DISTINCT FROM 'business'
        OR hotspot.evidence ->> 'replacement_point'
            IS DISTINCT FROM parent.replacement_point
    THEN
        RAISE EXCEPTION 'Formal Authority Context disagrees with its frozen Round';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER formal_authority_context_parent
BEFORE INSERT ON formal_round_authority_contexts
FOR EACH ROW EXECUTE FUNCTION enforce_formal_authority_context_parent();

CREATE OR REPLACE FUNCTION protect_formal_authority_parent_identity()
RETURNS TRIGGER AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM formal_round_authority_contexts
        WHERE round_id = OLD.round_id
    ) AND (
        OLD.task_id IS DISTINCT FROM NEW.task_id
        OR OLD.run_mode IS DISTINCT FROM NEW.run_mode
        OR OLD.project_mode IS DISTINCT FROM NEW.project_mode
        OR OLD.target_snapshot_id IS DISTINCT FROM NEW.target_snapshot_id
        OR OLD.stage0_run_id IS DISTINCT FROM NEW.stage0_run_id
        OR OLD.stage0_protocol_hash IS DISTINCT FROM NEW.stage0_protocol_hash
        OR OLD.baseline_epoch_id IS DISTINCT FROM NEW.baseline_epoch_id
        OR OLD.hotspot_id IS DISTINCT FROM NEW.hotspot_id
        OR OLD.replacement_point IS DISTINCT FROM NEW.replacement_point
        OR OLD.workload_id IS DISTINCT FROM NEW.workload_id
        OR OLD.workload_hash IS DISTINCT FROM NEW.workload_hash
        OR OLD.configuration_hash IS DISTINCT FROM NEW.configuration_hash
        OR OLD.image_digest IS DISTINCT FROM NEW.image_digest
        OR OLD.adapter_profile IS DISTINCT FROM NEW.adapter_profile
        OR OLD.candidate_family_hash IS DISTINCT FROM NEW.candidate_family_hash
        OR OLD.artifact_family_hash IS DISTINCT FROM NEW.artifact_family_hash
        OR OLD.search_plan_hash IS DISTINCT FROM NEW.search_plan_hash
        OR OLD.holdout_plan_commitment IS DISTINCT FROM NEW.holdout_plan_commitment
        OR OLD.holdout_plan_authority_id IS DISTINCT FROM NEW.holdout_plan_authority_id
        OR OLD.holdout_plan_authority_hash IS DISTINCT FROM NEW.holdout_plan_authority_hash
        OR OLD.selection_rule_hash IS DISTINCT FROM NEW.selection_rule_hash
        OR OLD.automatic_release_allowed IS DISTINCT FROM NEW.automatic_release_allowed
    ) THEN
        RAISE EXCEPTION 'sealed Formal Round authority identity is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER formal_authority_parent_identity_immutable
BEFORE UPDATE ON search_rounds
FOR EACH ROW EXECUTE FUNCTION protect_formal_authority_parent_identity();

CREATE TABLE formal_round_barriers (
    barrier_id UUID PRIMARY KEY,
    round_id UUID NOT NULL,
    authority_context_id UUID NOT NULL,
    authority_context_hash TEXT NOT NULL,
    phase TEXT NOT NULL,
    parent_search_barrier_id UUID,
    input_family_hash TEXT NOT NULL,
    holdout_family_hash TEXT,
    input_summary_hash TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    rule_hash TEXT NOT NULL,
    outcome TEXT NOT NULL,
    expected_member_count INTEGER NOT NULL,
    payload JSONB NOT NULL,
    payload_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    run_mode TEXT NOT NULL DEFAULT 'formal',
    synthetic BOOLEAN NOT NULL DEFAULT FALSE,
    automatic_release_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    closed_by TEXT NOT NULL,
    closed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT formal_barrier_context_fk FOREIGN KEY (
        round_id, authority_context_id, authority_context_hash
    ) REFERENCES formal_round_authority_contexts (
        round_id, authority_context_id, context_hash
    ) ON DELETE RESTRICT,
    CONSTRAINT formal_barrier_phase_known CHECK (phase IN ('search', 'holdout')),
    CONSTRAINT formal_barrier_outcome_known CHECK (
        outcome IN ('members_promoted', 'no_promotable_candidate', 'completed')
    ),
    CONSTRAINT formal_barrier_member_count_valid
        CHECK (expected_member_count BETWEEN 1 AND 4),
    CONSTRAINT formal_barrier_payload_object CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT formal_barrier_run_mode CHECK (run_mode = 'formal'),
    CONSTRAINT formal_barrier_non_synthetic CHECK (synthetic = FALSE),
    CONSTRAINT formal_barrier_never_auto_releases
        CHECK (automatic_release_allowed = FALSE),
    CONSTRAINT formal_barrier_family_shape CHECK (
        (
            phase = 'search'
            AND parent_search_barrier_id IS NULL
            AND holdout_family_hash IS NULL
            AND outcome IN ('members_promoted', 'no_promotable_candidate')
        ) OR (
            phase = 'holdout'
            AND parent_search_barrier_id IS NOT NULL
            AND holdout_family_hash IS NOT NULL
            AND input_family_hash = holdout_family_hash
            AND outcome = 'completed'
            AND expected_member_count <= 2
        )
    ),
    CONSTRAINT formal_barrier_all_hashes_valid CHECK (
        authority_context_hash ~ '^sha256:[0-9a-f]{64}$'
        AND input_family_hash ~ '^sha256:[0-9a-f]{64}$'
        AND (
            holdout_family_hash IS NULL
            OR holdout_family_hash ~ '^sha256:[0-9a-f]{64}$'
        )
        AND input_summary_hash ~ '^sha256:[0-9a-f]{64}$'
        AND rule_hash ~ '^sha256:[0-9a-f]{64}$'
        AND payload_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    UNIQUE (round_id, phase),
    UNIQUE (round_id, barrier_id, phase)
);

CREATE OR REPLACE FUNCTION enforce_formal_barrier_context()
RETURNS TRIGGER AS $$
DECLARE
    authority formal_round_authority_contexts%ROWTYPE;
    parent formal_round_barriers%ROWTYPE;
    reveal RECORD;
    round_parent search_rounds%ROWTYPE;
BEGIN
    SELECT * INTO authority
    FROM formal_round_authority_contexts
    WHERE round_id = NEW.round_id
      AND authority_context_id = NEW.authority_context_id
      AND context_hash = NEW.authority_context_hash;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Formal Barrier Authority Context does not exist';
    END IF;
    IF NEW.phase = 'search' AND NEW.input_family_hash <> authority.artifact_family_hash THEN
        RAISE EXCEPTION 'Formal Search Barrier must bind the frozen Artifact Family';
    END IF;
    SELECT * INTO round_parent FROM search_rounds WHERE round_id = NEW.round_id FOR SHARE;
    IF NEW.phase = 'search'
        AND NEW.expected_member_count IS DISTINCT FROM round_parent.declared_candidate_count
    THEN
        RAISE EXCEPTION 'Formal Search Barrier must include the declared Candidate Family';
    END IF;
    IF NEW.phase = 'holdout' THEN
        SELECT * INTO parent
        FROM formal_round_barriers
        WHERE round_id = NEW.round_id
          AND barrier_id = NEW.parent_search_barrier_id
          AND phase = 'search';
        IF NOT FOUND
            OR parent.authority_context_id <> NEW.authority_context_id
            OR parent.authority_context_hash <> NEW.authority_context_hash
            OR parent.outcome <> 'members_promoted'
        THEN
            RAISE EXCEPTION 'Formal Holdout Barrier requires its frozen Search parent';
        END IF;
        SELECT * INTO reveal FROM formal_round_holdout_reveals
        WHERE round_id = NEW.round_id;
        IF NOT FOUND
            OR reveal.authority_context_id <> NEW.authority_context_id
            OR reveal.authority_context_hash <> NEW.authority_context_hash
            OR reveal.search_barrier_id <> NEW.parent_search_barrier_id
            OR reveal.holdout_family_hash <> NEW.holdout_family_hash
            OR round_parent.holdout_family_hash <> NEW.holdout_family_hash
        THEN
            RAISE EXCEPTION 'Formal Holdout Barrier requires its frozen Reveal Family';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER formal_barrier_context
BEFORE INSERT ON formal_round_barriers
FOR EACH ROW EXECUTE FUNCTION enforce_formal_barrier_context();

CREATE TABLE formal_round_holdout_reveals (
    reveal_lease_id UUID PRIMARY KEY,
    round_id UUID NOT NULL UNIQUE,
    authority_context_id UUID NOT NULL,
    authority_context_hash TEXT NOT NULL,
    search_barrier_id UUID NOT NULL,
    search_phase TEXT NOT NULL DEFAULT 'search',
    fencing_token BIGINT NOT NULL,
    holdout_family_hash TEXT NOT NULL,
    holdout_plan_hash TEXT NOT NULL,
    reveal_evidence_uri TEXT NOT NULL,
    reveal_evidence_hash TEXT NOT NULL,
    revealed_by TEXT NOT NULL,
    revealed_at TIMESTAMPTZ NOT NULL,
    run_mode TEXT NOT NULL DEFAULT 'formal',
    synthetic BOOLEAN NOT NULL DEFAULT FALSE,
    automatic_release_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT formal_reveal_context_fk FOREIGN KEY (
        round_id, authority_context_id, authority_context_hash
    ) REFERENCES formal_round_authority_contexts (
        round_id, authority_context_id, context_hash
    ) ON DELETE RESTRICT,
    CONSTRAINT formal_reveal_search_barrier_fk FOREIGN KEY (
        round_id, search_barrier_id, search_phase
    ) REFERENCES formal_round_barriers (round_id, barrier_id, phase) ON DELETE RESTRICT,
    CONSTRAINT formal_reveal_search_phase CHECK (search_phase = 'search'),
    CONSTRAINT formal_reveal_fencing_positive CHECK (fencing_token >= 1),
    CONSTRAINT formal_reveal_file_uri CHECK (
        reveal_evidence_uri ~ '^(file:///|file://localhost/)[^?#]+$'
    ),
    CONSTRAINT formal_reveal_run_mode CHECK (run_mode = 'formal'),
    CONSTRAINT formal_reveal_non_synthetic CHECK (synthetic = FALSE),
    CONSTRAINT formal_reveal_never_auto_releases
        CHECK (automatic_release_allowed = FALSE),
    CONSTRAINT formal_reveal_all_hashes_valid CHECK (
        authority_context_hash ~ '^sha256:[0-9a-f]{64}$'
        AND holdout_family_hash ~ '^sha256:[0-9a-f]{64}$'
        AND holdout_plan_hash ~ '^sha256:[0-9a-f]{64}$'
        AND reveal_evidence_hash ~ '^sha256:[0-9a-f]{64}$'
    )
);

CREATE OR REPLACE FUNCTION enforce_formal_reveal_identity()
RETURNS TRIGGER AS $$
DECLARE
    authority formal_round_authority_contexts%ROWTYPE;
    barrier formal_round_barriers%ROWTYPE;
    round_parent search_rounds%ROWTYPE;
BEGIN
    SELECT * INTO authority FROM formal_round_authority_contexts
    WHERE round_id = NEW.round_id
      AND authority_context_id = NEW.authority_context_id
      AND context_hash = NEW.authority_context_hash;
    SELECT * INTO barrier FROM formal_round_barriers
    WHERE round_id = NEW.round_id
      AND barrier_id = NEW.search_barrier_id
      AND phase = 'search';
    SELECT * INTO round_parent FROM search_rounds
    WHERE round_id = NEW.round_id FOR SHARE;
    IF authority.authority_context_id IS NULL
        OR barrier.barrier_id IS NULL
        OR barrier.authority_context_id <> NEW.authority_context_id
        OR barrier.authority_context_hash <> NEW.authority_context_hash
        OR barrier.outcome <> 'members_promoted'
        OR round_parent.holdout_family_hash <> NEW.holdout_family_hash
        OR round_parent.holdout_plan_hash <> NEW.holdout_plan_hash
        OR round_parent.holdout_reveal_lease_id <> NEW.reveal_lease_id
        OR round_parent.holdout_reveal_evidence_hash <> NEW.reveal_evidence_hash
    THEN
        RAISE EXCEPTION 'Formal Holdout Reveal disagrees with Round or Search Barrier';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER formal_reveal_identity
BEFORE INSERT ON formal_round_holdout_reveals
FOR EACH ROW EXECUTE FUNCTION enforce_formal_reveal_identity();

CREATE TABLE formal_multiple_comparison_results (
    multiple_comparison_id UUID PRIMARY KEY,
    round_id UUID NOT NULL UNIQUE,
    authority_context_id UUID NOT NULL,
    authority_context_hash TEXT NOT NULL,
    holdout_barrier_id UUID NOT NULL UNIQUE,
    holdout_phase TEXT NOT NULL DEFAULT 'holdout',
    holdout_family_hash TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    protocol_hash TEXT NOT NULL,
    result_hash TEXT NOT NULL UNIQUE,
    payload JSONB NOT NULL,
    payload_hash TEXT NOT NULL,
    run_mode TEXT NOT NULL DEFAULT 'formal',
    synthetic BOOLEAN NOT NULL DEFAULT FALSE,
    automatic_release_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT formal_fwer_context_fk FOREIGN KEY (
        round_id, authority_context_id, authority_context_hash
    ) REFERENCES formal_round_authority_contexts (
        round_id, authority_context_id, context_hash
    ) ON DELETE RESTRICT,
    CONSTRAINT formal_fwer_holdout_barrier_fk FOREIGN KEY (
        round_id, holdout_barrier_id, holdout_phase
    ) REFERENCES formal_round_barriers (round_id, barrier_id, phase) ON DELETE RESTRICT,
    CONSTRAINT formal_fwer_holdout_phase CHECK (holdout_phase = 'holdout'),
    CONSTRAINT formal_fwer_payload_object CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT formal_fwer_run_mode CHECK (run_mode = 'formal'),
    CONSTRAINT formal_fwer_non_synthetic CHECK (synthetic = FALSE),
    CONSTRAINT formal_fwer_never_auto_releases
        CHECK (automatic_release_allowed = FALSE),
    CONSTRAINT formal_fwer_all_hashes_valid CHECK (
        authority_context_hash ~ '^sha256:[0-9a-f]{64}$'
        AND holdout_family_hash ~ '^sha256:[0-9a-f]{64}$'
        AND protocol_hash ~ '^sha256:[0-9a-f]{64}$'
        AND result_hash ~ '^sha256:[0-9a-f]{64}$'
        AND payload_hash ~ '^sha256:[0-9a-f]{64}$'
    )
);

CREATE OR REPLACE FUNCTION enforce_formal_fwer_family()
RETURNS TRIGGER AS $$
DECLARE
    barrier formal_round_barriers%ROWTYPE;
BEGIN
    SELECT * INTO barrier
    FROM formal_round_barriers
    WHERE round_id = NEW.round_id
      AND barrier_id = NEW.holdout_barrier_id
      AND phase = 'holdout';
    IF NOT FOUND
        OR barrier.authority_context_id <> NEW.authority_context_id
        OR barrier.authority_context_hash <> NEW.authority_context_hash
        OR barrier.holdout_family_hash <> NEW.holdout_family_hash
    THEN
        RAISE EXCEPTION 'Formal FWER must bind the frozen Holdout Barrier Family';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER formal_fwer_family
BEFORE INSERT ON formal_multiple_comparison_results
FOR EACH ROW EXECUTE FUNCTION enforce_formal_fwer_family();

CREATE TABLE formal_round_evidence_bundles (
    round_evidence_bundle_id UUID PRIMARY KEY,
    round_id UUID NOT NULL UNIQUE,
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    authority_context_id UUID NOT NULL,
    authority_context_hash TEXT NOT NULL,
    evidence_store_id TEXT NOT NULL,
    evidence_store_hash TEXT NOT NULL,
    terminal_reason TEXT NOT NULL,
    candidate_family_hash TEXT NOT NULL,
    artifact_family_hash TEXT NOT NULL,
    holdout_family_hash TEXT,
    search_plan_hash TEXT NOT NULL,
    holdout_plan_commitment TEXT NOT NULL,
    holdout_plan_hash TEXT,
    holdout_reveal_evidence_hash TEXT,
    search_barrier_id UUID NOT NULL,
    search_phase TEXT NOT NULL DEFAULT 'search',
    holdout_barrier_id UUID,
    multiple_comparison_id UUID,
    evidence_index_uri TEXT NOT NULL,
    evidence_index_hash TEXT NOT NULL,
    budget_ledger_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    payload_hash TEXT NOT NULL,
    run_mode TEXT NOT NULL DEFAULT 'formal',
    synthetic BOOLEAN NOT NULL DEFAULT FALSE,
    automatic_release_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT formal_evidence_store_context_fk FOREIGN KEY (
        round_id, authority_context_id, authority_context_hash, task_id,
        evidence_store_id, evidence_store_hash
    ) REFERENCES formal_round_authority_contexts (
        round_id, authority_context_id, context_hash, task_id,
        evidence_store_id, evidence_store_hash
    ) ON DELETE RESTRICT,
    CONSTRAINT formal_evidence_search_barrier_fk FOREIGN KEY (
        round_id, search_barrier_id, search_phase
    ) REFERENCES formal_round_barriers (round_id, barrier_id, phase) ON DELETE RESTRICT,
    CONSTRAINT formal_evidence_search_phase CHECK (search_phase = 'search'),
    CONSTRAINT formal_evidence_holdout_barrier_fk FOREIGN KEY (holdout_barrier_id)
        REFERENCES formal_round_barriers(barrier_id) ON DELETE RESTRICT,
    CONSTRAINT formal_evidence_fwer_fk FOREIGN KEY (multiple_comparison_id)
        REFERENCES formal_multiple_comparison_results(multiple_comparison_id)
        ON DELETE RESTRICT,
    CONSTRAINT formal_evidence_terminal_reason_known CHECK (
        terminal_reason IN ('holdout_completed', 'no_promotable_candidate')
    ),
    CONSTRAINT formal_evidence_holdout_shape CHECK (
        (
            terminal_reason = 'no_promotable_candidate'
            AND holdout_family_hash IS NULL
            AND holdout_plan_hash IS NULL
            AND holdout_reveal_evidence_hash IS NULL
            AND holdout_barrier_id IS NULL
            AND multiple_comparison_id IS NULL
        ) OR (
            terminal_reason = 'holdout_completed'
            AND holdout_family_hash IS NOT NULL
            AND holdout_plan_hash IS NOT NULL
            AND holdout_reveal_evidence_hash IS NOT NULL
            AND holdout_barrier_id IS NOT NULL
            AND multiple_comparison_id IS NOT NULL
        )
    ),
    CONSTRAINT formal_evidence_index_file_uri CHECK (
        evidence_index_uri ~ '^(file:///|file://localhost/)[^?#]+$'
    ),
    CONSTRAINT formal_evidence_payload_object CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT formal_evidence_run_mode CHECK (run_mode = 'formal'),
    CONSTRAINT formal_evidence_non_synthetic CHECK (synthetic = FALSE),
    CONSTRAINT formal_evidence_never_auto_releases
        CHECK (automatic_release_allowed = FALSE),
    CONSTRAINT formal_evidence_all_hashes_valid CHECK (
        authority_context_hash ~ '^sha256:[0-9a-f]{64}$'
        AND evidence_store_hash ~ '^sha256:[0-9a-f]{64}$'
        AND candidate_family_hash ~ '^sha256:[0-9a-f]{64}$'
        AND artifact_family_hash ~ '^sha256:[0-9a-f]{64}$'
        AND (
            holdout_family_hash IS NULL
            OR holdout_family_hash ~ '^sha256:[0-9a-f]{64}$'
        )
        AND search_plan_hash ~ '^sha256:[0-9a-f]{64}$'
        AND holdout_plan_commitment ~ '^sha256:[0-9a-f]{64}$'
        AND (
            holdout_plan_hash IS NULL
            OR holdout_plan_hash ~ '^sha256:[0-9a-f]{64}$'
        )
        AND (
            holdout_reveal_evidence_hash IS NULL
            OR holdout_reveal_evidence_hash ~ '^sha256:[0-9a-f]{64}$'
        )
        AND evidence_index_hash ~ '^sha256:[0-9a-f]{64}$'
        AND budget_ledger_hash ~ '^sha256:[0-9a-f]{64}$'
        AND payload_hash ~ '^sha256:[0-9a-f]{64}$'
    )
);

CREATE OR REPLACE FUNCTION enforce_formal_evidence_identity()
RETURNS TRIGGER AS $$
DECLARE
    authority formal_round_authority_contexts%ROWTYPE;
    search formal_round_barriers%ROWTYPE;
    reveal formal_round_holdout_reveals%ROWTYPE;
    holdout formal_round_barriers%ROWTYPE;
    fwer formal_multiple_comparison_results%ROWTYPE;
BEGIN
    SELECT * INTO authority
    FROM formal_round_authority_contexts
    WHERE round_id = NEW.round_id
      AND authority_context_id = NEW.authority_context_id
      AND context_hash = NEW.authority_context_hash;
    IF NOT FOUND
        OR authority.candidate_family_hash <> NEW.candidate_family_hash
        OR authority.artifact_family_hash <> NEW.artifact_family_hash
        OR authority.search_plan_hash <> NEW.search_plan_hash
        OR authority.holdout_plan_commitment <> NEW.holdout_plan_commitment
        OR authority.task_id <> NEW.task_id
    THEN
        RAISE EXCEPTION 'Formal Evidence Bundle disagrees with Authority Context';
    END IF;
    SELECT * INTO search
    FROM formal_round_barriers
    WHERE round_id = NEW.round_id
      AND barrier_id = NEW.search_barrier_id
      AND phase = 'search';
    IF search.barrier_id IS NULL
        OR search.authority_context_id <> NEW.authority_context_id
        OR search.authority_context_hash <> NEW.authority_context_hash
        OR (
            NEW.terminal_reason = 'no_promotable_candidate'
            AND search.outcome <> 'no_promotable_candidate'
        )
        OR (
            NEW.terminal_reason = 'holdout_completed'
            AND search.outcome <> 'members_promoted'
        )
    THEN
        RAISE EXCEPTION 'Formal Evidence Bundle disagrees with Search Barrier';
    END IF;
    IF NEW.terminal_reason = 'holdout_completed' THEN
        SELECT * INTO reveal
        FROM formal_round_holdout_reveals
        WHERE round_id = NEW.round_id;
        SELECT * INTO holdout
        FROM formal_round_barriers
        WHERE round_id = NEW.round_id
          AND barrier_id = NEW.holdout_barrier_id
          AND phase = 'holdout';
        SELECT * INTO fwer
        FROM formal_multiple_comparison_results
        WHERE round_id = NEW.round_id
          AND multiple_comparison_id = NEW.multiple_comparison_id;
        IF holdout.barrier_id IS NULL
            OR reveal.reveal_lease_id IS NULL
            OR fwer.multiple_comparison_id IS NULL
            OR reveal.authority_context_id <> NEW.authority_context_id
            OR reveal.authority_context_hash <> NEW.authority_context_hash
            OR reveal.search_barrier_id <> NEW.search_barrier_id
            OR reveal.holdout_family_hash <> NEW.holdout_family_hash
            OR reveal.holdout_plan_hash <> NEW.holdout_plan_hash
            OR reveal.reveal_evidence_hash <> NEW.holdout_reveal_evidence_hash
            OR holdout.authority_context_id <> NEW.authority_context_id
            OR holdout.authority_context_hash <> NEW.authority_context_hash
            OR holdout.holdout_family_hash <> NEW.holdout_family_hash
            OR fwer.authority_context_id <> NEW.authority_context_id
            OR fwer.authority_context_hash <> NEW.authority_context_hash
            OR fwer.holdout_barrier_id <> NEW.holdout_barrier_id
            OR fwer.holdout_family_hash <> NEW.holdout_family_hash
        THEN
            RAISE EXCEPTION 'Formal Evidence Bundle disagrees with Holdout/FWER';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER formal_evidence_identity
BEFORE INSERT ON formal_round_evidence_bundles
FOR EACH ROW EXECUTE FUNCTION enforce_formal_evidence_identity();

CREATE OR REPLACE FUNCTION reject_m2_formal_authority_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'M2 Formal Authority evidence is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER formal_authority_context_append_only
BEFORE UPDATE OR DELETE ON formal_round_authority_contexts
FOR EACH ROW EXECUTE FUNCTION reject_m2_formal_authority_mutation();

CREATE TRIGGER formal_round_barriers_append_only
BEFORE UPDATE OR DELETE ON formal_round_barriers
FOR EACH ROW EXECUTE FUNCTION reject_m2_formal_authority_mutation();

CREATE TRIGGER formal_round_holdout_reveals_append_only
BEFORE UPDATE OR DELETE ON formal_round_holdout_reveals
FOR EACH ROW EXECUTE FUNCTION reject_m2_formal_authority_mutation();

CREATE TRIGGER formal_multiple_comparison_results_append_only
BEFORE UPDATE OR DELETE ON formal_multiple_comparison_results
FOR EACH ROW EXECUTE FUNCTION reject_m2_formal_authority_mutation();

CREATE TRIGGER formal_round_evidence_bundles_append_only
BEFORE UPDATE OR DELETE ON formal_round_evidence_bundles
FOR EACH ROW EXECUTE FUNCTION reject_m2_formal_authority_mutation();

INSERT INTO schema_migrations (version, name)
VALUES (12, 'm2_formal_authority')
ON CONFLICT (version) DO NOTHING;
