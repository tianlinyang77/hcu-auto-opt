# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from hcuopt.storage.migrations import MIGRATIONS, migration_plan, migration_sql


def test_m2_search_round_migration_is_registered_last() -> None:
    assert MIGRATIONS[8] == "0008_m2_search_round.sql"
    assert MIGRATIONS[9] == "0009_m2_round_authority.sql"
    assert MIGRATIONS[10] == "0010_operator_plan_preview.sql"
    assert MIGRATIONS[11] == "0011_operator_start_intent.sql"
    assert MIGRATIONS[12] == "0012_m2_formal_authority.sql"
    assert MIGRATIONS[13] == "0013_m2_formal_finalizer.sql"
    assert MIGRATIONS[14] == "0014_m2_formal_signoff_outbox.sql"
    assert MIGRATIONS[15] == "0015_m2b_agent_generation_authority.sql"
    assert MIGRATIONS[16] == "0016_m2b_runner_execution_receipt.sql"
    assert MIGRATIONS[17] == "0017_m2b_agent_evidence_read_model.sql"
    assert MIGRATIONS[18] == "0018_m2a_formal_start_intent.sql"
    assert MIGRATIONS[19] == "0019_m2b_terminal_runner_receipt_binding.sql"
    assert MIGRATIONS[20] == "0020_m2a_formal_evidence_acceptance.sql"
    assert MIGRATIONS[21] == "0021_m2a_formal_evaluation_registration.sql"
    assert MIGRATIONS[22] == "0022_endpoint_validation_control_plane.sql"
    assert MIGRATIONS[23] == "0023_endpoint_validation_failure_evidence.sql"
    assert MIGRATIONS[24] == "0024_endpoint_campaign_control_plane.sql"
    assert MIGRATIONS[25] == "0025_endpoint_adjudication_jobs.sql"
    assert [version for version, _ in migration_plan()] == list(range(1, 26))


def test_endpoint_failure_migration_preserves_error_and_cleanup_evidence() -> None:
    sql = migration_sql(23)

    assert "ADD COLUMN failure_error JSONB" in sql
    assert "ADD COLUMN cleanup_evidence JSONB" in sql
    assert "VALUES (23, 'endpoint_validation_failure_evidence')" in sql


def test_terminal_runner_receipt_binding_migration_freezes_settlement_evidence() -> None:
    sql = migration_sql(19)

    assert "agent_generator_attempt_runner_receipt_state_binding" in sql
    assert "Runner Receipt can only bind at settlement" in sql
    assert "Runner Receipt binding cannot change" in sql
    assert "terminal evidence cannot change" in sql
    assert "VALUES (19, 'm2b_terminal_runner_receipt_binding')" in sql


def test_formal_acceptance_migration_is_content_addressed_and_append_only() -> None:
    sql = migration_sql(20)

    assert "CREATE TABLE formal_evidence_acceptance_snapshots" in sql
    assert "CREATE TABLE formal_evidence_acceptance_reviews" in sql
    assert "snapshot_hash TEXT NOT NULL UNIQUE" in sql
    assert "review_hash TEXT NOT NULL UNIQUE" in sql
    assert "formal_evidence_acceptance_snapshots_append_only" in sql
    assert "formal_evidence_acceptance_reviews_append_only" in sql
    assert "owner_window_authorization' = 'not_granted'" in sql
    assert "automatic_release_allowed' = 'false'" in sql
    assert "VALUES (20, 'm2a_formal_evidence_acceptance')" in sql


def test_m2_search_round_migration_contains_a_line_authorities() -> None:
    sql = migration_sql(8)

    for table in (
        "search_rounds",
        "round_candidates",
        "round_budget_reservations",
        "round_budget_ledger",
    ):
        assert f"CREATE TABLE {table}" in sql

    assert "idempotency_key TEXT NOT NULL UNIQUE" in sql
    assert "search_rounds_one_active_per_task_idx" in sql
    assert "round_budget_ledger_terminal_idx" in sql
    assert "round_budget_ledger_append_only" in sql
    assert "VALUES (8, 'm2_search_round')" in sql


def test_m2_search_round_migration_keeps_measurement_and_verdict_out_of_a_line() -> None:
    sql = migration_sql(8)

    assert "round_measurements" not in sql
    assert "multiple_comparison_results" not in sql
    assert "round_evidence_bundles" not in sql


def test_m2_round_authority_migration_is_append_only_and_scripted_safe() -> None:
    sql = migration_sql(9)

    for table in (
        "round_barriers",
        "round_holdout_reveals",
        "multiple_comparison_results",
        "round_evidence_bundles",
    ):
        assert f"CREATE TABLE {table}" in sql

    assert "UNIQUE (round_id, phase)" in sql
    assert "round_barriers_append_only" in sql
    assert "round_holdout_reveals_append_only" in sql
    assert "multiple_comparison_results_append_only" in sql
    assert "round_evidence_bundles_append_only" in sql
    assert "round_evidence_never_auto_releases" in sql
    assert "VALUES (9, 'm2_round_authority')" in sql


def test_operator_preview_migration_is_immutable_and_scripted_safe() -> None:
    sql = migration_sql(10)

    assert "CREATE TABLE operator_plan_previews" in sql
    assert "idempotency_key TEXT NOT NULL UNIQUE" in sql
    assert "operator_plan_previews_append_only" in sql
    assert "operator_preview_scripted_only" in sql
    assert "operator_preview_never_auto_releases" in sql
    assert "operator_preview_payload_id_matches" in sql
    assert "operator_preview_payload_request_digest_matches" in sql
    assert "operator_preview_payload_plan_hash_matches" in sql
    assert "operator_preview_payload_safety_matches" in sql
    assert "VALUES (10, 'operator_plan_preview')" in sql


def test_operator_start_migration_is_durable_and_scripted_safe() -> None:
    sql = migration_sql(11)

    assert "CREATE TABLE operator_start_intents" in sql
    assert "preview_id UUID NOT NULL UNIQUE" in sql
    assert "idempotency_key TEXT NOT NULL UNIQUE" in sql
    assert "operator_start_plan_fields_atomic" in sql
    assert "operator_start_family_matches_state" in sql
    assert "operator_start_never_auto_releases" in sql
    assert "VALUES (11, 'operator_start_intent')" in sql


def test_formal_start_migration_is_separate_nonexecuting_and_audited() -> None:
    sql = migration_sql(18)

    assert "CREATE TABLE formal_operator_start_intents" in sql
    assert "CREATE TABLE formal_operator_start_intent_events" in sql
    assert "formal_start_nonexecuting_slice" in sql
    assert "round_creation_allowed = FALSE" in sql
    assert "hcu_accessed = FALSE" in sql
    assert "synthetic = FALSE" in sql
    assert "formal_start_events_append_only" in sql
    assert "formal_start_event_type_valid" in sql
    assert "formal_start_event_error_state" in sql
    assert "VALUES (18, 'm2a_formal_start_intent')" in sql


def test_m2_formal_authority_migration_is_separate_append_only_and_fail_closed() -> None:
    scripted = migration_sql(9)
    formal = migration_sql(12)

    assert "round_barrier_scripted_only CHECK (synthetic = TRUE)" in scripted
    assert "round_evidence_scripted_only CHECK (synthetic = TRUE)" in scripted
    for table in (
        "formal_round_authority_contexts",
        "formal_round_barriers",
        "formal_round_holdout_reveals",
        "formal_multiple_comparison_results",
        "formal_round_evidence_bundles",
    ):
        assert f"CREATE TABLE {table}" in formal

    assert "formal_authority_non_synthetic CHECK (synthetic = FALSE)" in formal
    assert "formal_barrier_non_synthetic CHECK (synthetic = FALSE)" in formal
    assert "formal_evidence_non_synthetic CHECK (synthetic = FALSE)" in formal
    assert "formal_evidence_never_auto_releases" in formal
    assert "reject_m2_formal_authority_mutation" in formal
    assert "nonce" not in formal.lower()
    assert "VALUES (12, 'm2_formal_authority')" in formal


def test_m2_formal_finalizer_migration_binds_search_output_family_atomically() -> None:
    sql = migration_sql(13)

    assert "DROP CONSTRAINT formal_barrier_family_shape" in sql
    assert "outcome = 'members_promoted'" in sql
    assert "holdout_family_hash IS NOT NULL" in sql
    assert "round_parent.holdout_family_hash IS DISTINCT FROM NEW.holdout_family_hash" in sql
    assert "VALUES (13, 'm2_formal_finalizer')" in sql


def test_m2_formal_signoff_migration_is_two_phase_and_never_releases() -> None:
    sql = migration_sql(14)

    for table in (
        "formal_round_signoff_intents",
        "formal_round_signoff_outbox",
        "formal_round_signoffs",
    ):
        assert f"CREATE TABLE {table}" in sql

    assert "state IN ('preparing', 'artifact_published', 'finalized', 'failed')" in sql
    assert "formal_signoff_intent_run_mode CHECK (run_mode = 'formal')" in sql
    assert "formal_signoff_intent_non_synthetic CHECK (synthetic = FALSE)" in sql
    assert "formal_signoff_outbox_publication_atomic" in sql
    assert "formal_round_signoff_never_auto_releases" in sql
    assert "formal_round_signoff_append_only" in sql
    assert "VALUES (14, 'm2_formal_signoff_outbox')" in sql


def test_m2b_agent_generation_migration_is_isolated_recoverable_and_never_releases() -> None:
    sql = migration_sql(15)

    for table in (
        "agent_generation_runs",
        "agent_generator_attempts",
        "agent_generation_budget_ledger",
        "agent_candidate_proposal_refs",
    ):
        assert f"CREATE TABLE {table}" in sql

    assert "agent_generator_attempt_pending_idx" in sql
    assert "agent_generator_attempt_lease_idx" in sql
    assert "agent_generation_budget_ledger_append_only" in sql
    assert "agent_generation_run_never_auto_releases" in sql
    assert "agent_generator_attempt_no_hcu" in sql
    assert "agent_generator_attempt_no_measurement" in sql
    assert "agent_candidate_proposal_not_measured" in sql
    assert "round_budget" not in sql
    assert "round_candidates" not in sql
    assert "VALUES (15, 'm2b_agent_generation_authority')" in sql
