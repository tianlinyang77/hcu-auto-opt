# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Read-only schema diagnostics, not authority or permission to start a worker."""

import json
import os

import psycopg

from hcuopt.storage.migrations import MIGRATIONS

FORMAL_TABLES = (
    "formal_round_dispatches", "formal_round_dispatch_events",
    "formal_dispatch_claims", "formal_dispatch_claim_events",
    "formal_dispatch_stop_requests", "formal_phase_journal",
    "formal_phase_journal_events", "formal_build_journal",
)


def inspect_formal_schema(database_url: str) -> dict[str, object]:
    """Use one read-only snapshot; return no DSN, credentials or business rows.

    Inspect only current_schema(), so a missing object cannot silently resolve
    through a later search_path entry. Never migrate or initialize a repository.
    This is a presence/type check, not a schema-definition integrity attestation.
    """
    with psycopg.connect(database_url, connect_timeout=5) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '5s'")
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        if schema is None:
            raise ValueError("database has no current schema")
        tables = {
            row[0] for row in conn.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=%s AND c.relkind IN ('r','p')", (schema,),
            ).fetchall()
        }
        versions = []
        if "schema_migrations" in tables:
            versions = [row[0] for row in conn.execute(
                psycopg.sql.SQL("SELECT version FROM {}.schema_migrations ORDER BY version")
                .format(psycopg.sql.Identifier(schema))
            ).fetchall()]
        lane = conn.execute(
            "SELECT data_type,is_nullable,column_default FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name='jobs' AND column_name='execution_lane'",
            (schema,),
        ).fetchone()
        missing = [name for name in FORMAL_TABLES if name not in tables]
        blockers = []
        if versions != sorted(MIGRATIONS):
            blockers.append("migration_sequence_mismatch")
        if missing:
            blockers.append("formal_tables_missing")
        if lane != ("text", "NO", "'general'::text"):
            blockers.append("job_lane_definition_mismatch")
        return {
            "schema_version": "formal-schema-inspection-v1",
            "database_schema": schema,
            "applied_migrations": versions,
            "expected_migrations": sorted(MIGRATIONS),
            "missing_tables": missing,
            "blockers": blockers,
            "schema_checks_passed": not blockers,
            "execution_authorized": False,
            "automatic_release_allowed": False,
            "scope": "schema_presence_only_not_deployment_acceptance",
        }


def main() -> int:
    dsn = os.environ.get("HCUOPT_DATABASE_URL")
    if not dsn:
        print(json.dumps({"error": "HCUOPT_DATABASE_URL is required"}))
        return 2
    try:
        report = inspect_formal_schema(dsn)
    except Exception:
        # Driver exceptions can include host, user, connection strings or SQL.
        print(json.dumps({"error": "schema inspection failed; no deployment authorized"}))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["schema_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
