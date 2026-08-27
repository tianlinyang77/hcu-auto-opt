CREATE TABLE round_barriers (
    barrier_id UUID PRIMARY KEY,
    round_id UUID NOT NULL REFERENCES search_rounds(round_id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    input_family_hash TEXT NOT NULL,
    input_summary_hash TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    rule_hash TEXT NOT NULL,
    outcome TEXT NOT NULL,
    expected_member_count INTEGER NOT NULL,
    payload JSONB NOT NULL,
    payload_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    synthetic BOOLEAN NOT NULL,
    closed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT round_barrier_phase_known CHECK (phase IN ('search', 'holdout')),
    CONSTRAINT round_barrier_outcome_known CHECK (
        outcome IN ('members_promoted', 'no_promotable_candidate', 'completed')
    ),
    CONSTRAINT round_barrier_member_count_valid
        CHECK (expected_member_count BETWEEN 1 AND 4),
    CONSTRAINT round_barrier_payload_object CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT round_barrier_scripted_only CHECK (synthetic = TRUE),
    CONSTRAINT round_barrier_input_family_hash_valid
        CHECK (input_family_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT round_barrier_input_summary_hash_valid
        CHECK (input_summary_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT round_barrier_rule_hash_valid
        CHECK (rule_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT round_barrier_payload_hash_valid
        CHECK (payload_hash ~ '^sha256:[0-9a-f]{64}$'),
    UNIQUE (round_id, phase)
);

CREATE INDEX round_barriers_round_idx
ON round_barriers (round_id, phase, created_at);

CREATE TABLE round_holdout_reveals (
    reveal_lease_id UUID PRIMARY KEY,
    round_id UUID NOT NULL UNIQUE REFERENCES search_rounds(round_id) ON DELETE CASCADE,
    holdout_family_hash TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    reveal_evidence_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    payload_hash TEXT NOT NULL,
    synthetic BOOLEAN NOT NULL,
    revealed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT round_holdout_reveal_payload_object
        CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT round_holdout_reveal_scripted_only CHECK (synthetic = TRUE),
    CONSTRAINT round_holdout_reveal_family_hash_valid
        CHECK (holdout_family_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT round_holdout_reveal_plan_hash_valid
        CHECK (plan_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT round_holdout_reveal_evidence_hash_valid
        CHECK (reveal_evidence_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT round_holdout_reveal_payload_hash_valid
        CHECK (payload_hash ~ '^sha256:[0-9a-f]{64}$')
);

CREATE TABLE multiple_comparison_results (
    multiple_comparison_id UUID PRIMARY KEY,
    round_id UUID NOT NULL UNIQUE REFERENCES search_rounds(round_id) ON DELETE CASCADE,
    holdout_barrier_id UUID NOT NULL UNIQUE
        REFERENCES round_barriers(barrier_id) ON DELETE RESTRICT,
    holdout_family_hash TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    protocol_hash TEXT NOT NULL,
    result_hash TEXT NOT NULL UNIQUE,
    payload JSONB NOT NULL,
    payload_hash TEXT NOT NULL,
    synthetic BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT multiple_comparison_payload_object
        CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT multiple_comparison_scripted_only CHECK (synthetic = TRUE),
    CONSTRAINT multiple_comparison_family_hash_valid
        CHECK (holdout_family_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT multiple_comparison_protocol_hash_valid
        CHECK (protocol_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT multiple_comparison_result_hash_valid
        CHECK (result_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT multiple_comparison_payload_hash_valid
        CHECK (payload_hash ~ '^sha256:[0-9a-f]{64}$')
);

CREATE TABLE round_evidence_bundles (
    round_evidence_bundle_id UUID PRIMARY KEY,
    round_id UUID NOT NULL UNIQUE REFERENCES search_rounds(round_id) ON DELETE CASCADE,
    terminal_reason TEXT NOT NULL,
    evidence_index_uri TEXT NOT NULL,
    evidence_index_hash TEXT NOT NULL,
    budget_ledger_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    payload_hash TEXT NOT NULL,
    synthetic BOOLEAN NOT NULL,
    automatic_release_allowed BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT round_evidence_terminal_reason_known CHECK (
        terminal_reason IN ('holdout_completed', 'no_promotable_candidate')
    ),
    CONSTRAINT round_evidence_payload_object CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT round_evidence_scripted_only CHECK (synthetic = TRUE),
    CONSTRAINT round_evidence_never_auto_releases
        CHECK (automatic_release_allowed = FALSE),
    CONSTRAINT round_evidence_index_hash_valid
        CHECK (evidence_index_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT round_evidence_budget_hash_valid
        CHECK (budget_ledger_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT round_evidence_payload_hash_valid
        CHECK (payload_hash ~ '^sha256:[0-9a-f]{64}$')
);

CREATE OR REPLACE FUNCTION reject_m2_round_authority_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'M2 Round authority evidence is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER round_barriers_append_only
BEFORE UPDATE OR DELETE ON round_barriers
FOR EACH ROW EXECUTE FUNCTION reject_m2_round_authority_mutation();

CREATE TRIGGER round_holdout_reveals_append_only
BEFORE UPDATE OR DELETE ON round_holdout_reveals
FOR EACH ROW EXECUTE FUNCTION reject_m2_round_authority_mutation();

CREATE TRIGGER multiple_comparison_results_append_only
BEFORE UPDATE OR DELETE ON multiple_comparison_results
FOR EACH ROW EXECUTE FUNCTION reject_m2_round_authority_mutation();

CREATE TRIGGER round_evidence_bundles_append_only
BEFORE UPDATE OR DELETE ON round_evidence_bundles
FOR EACH ROW EXECUTE FUNCTION reject_m2_round_authority_mutation();

INSERT INTO schema_migrations (version, name)
VALUES (9, 'm2_round_authority')
ON CONFLICT (version) DO NOTHING;
