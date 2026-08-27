# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from hcuopt.storage.migrations import MIGRATIONS, migration_plan, migration_sql


def test_m2_search_round_migration_is_registered_last() -> None:
    assert MIGRATIONS[8] == "0008_m2_search_round.sql"
    assert MIGRATIONS[9] == "0009_m2_round_authority.sql"
    assert [version for version, _ in migration_plan()] == list(range(1, 10))


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
