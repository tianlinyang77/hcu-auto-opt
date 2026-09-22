# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

from hcuopt.contracts.m2 import (
    BudgetUsage,
    RoundBudgetLedgerEntry,
    RoundBudgetReservation,
    RoundCandidate,
)
from hcuopt.contracts.platform_v1 import SourceSnapshot
from hcuopt.contracts.v1 import WorkerRegister
from hcuopt.domain.errors import Conflict
from hcuopt.storage.formal_build_jobs import PostgresFormalBuildJobs
from hcuopt.storage.formal_build_journal import PostgresFormalBuildJournal
from hcuopt.workers.formal_build_consumer import FormalBuildConsumer
from tests.integration.test_formal_build_postgres import build_record_case  # noqa: F401
from tests.integration.test_formal_dispatch_postgres import dispatch_case  # noqa: F401
from tests.integration.test_formal_start_management_postgres import isolated_dsn  # noqa: F401

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.getenv("HCUOPT_DATABASE_URL"), reason="requires PostgreSQL"),
]
INPUT_HASH = "sha256:" + "a" * 64


@pytest.fixture
def journal_case(build_record_case):  # noqa: F811
    store, intent_id, token, result = build_record_case
    repo = store.claims.dispatcher.repository
    intent = repo.get_formal_start_intent(intent_id)
    with repo.connection() as conn:
        # Older dispatch fixtures use a SHA-prefixed placeholder for a Git tree.
        # Make this isolated fixture satisfy SourceSnapshot; do not relax validation.
        conn.execute(
            "UPDATE source_snapshots SET tree_hash = %s WHERE kind = 'baseline'",
            ("a" * 40,),
        )
        profile = conn.execute("SELECT adapter_profile FROM search_rounds").fetchone()[
            "adapter_profile"
        ]
    repo.register_worker(WorkerRegister(
        worker_id="builder", worker_type="build", adapter_profile=profile,
    ))
    jobs = PostgresFormalBuildJobs(store.claims, intent_id, "builder", token)
    job = jobs.enqueue(result.build.candidate_id)
    assert jobs.enqueue(result.build.candidate_id) == job
    assert repo.claim_job("builder") is None
    job_id = job["job_id"]
    reservation = RoundBudgetReservation(
        reservation_id=uuid4(), round_id=intent.round_id, job_id=job_id, attempt=1,
        candidate_id=result.build.candidate_id, planned=BudgetUsage(build_attempts=1),
        state="reserved", idempotency_key=f"journal-reserve:{job_id}",
    )
    entry = RoundBudgetLedgerEntry(
        ledger_entry_id=uuid4(), reservation_id=reservation.reservation_id,
        round_id=intent.round_id, entry_type="reserve", reserved=reservation.planned,
        actual=BudgetUsage(), lease_held_seconds=0, harness_active_seconds=0,
        raw_usage_evidence_hash=INPUT_HASH, idempotency_key=f"journal-ledger:{job_id}",
        created_at=datetime.now(timezone.utc),
    )
    repo.reserve_round_budget(reservation, entry)
    journal = PostgresFormalBuildJournal(store.claims, intent_id, "builder", token)
    return journal, reservation, result, store


def test_one_invocation_under_concurrency_and_changed_input_refused(journal_case):
    journal, reservation, result, _ = journal_case
    args = (result.build.candidate_id, reservation.reservation_id, INPUT_HASH)
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(journal.begin, *args) for _ in range(2)]
        assert sum(call.result()[1] for call in calls) == 1
    with pytest.raises(Conflict, match="another input"):
        journal.begin(*args[:2], "sha256:" + "b" * 64)
    journal.mark_unknown(*args)
    row, acquired = journal.begin(*args)
    assert not acquired and row["state"] == "recovery_required"
    with pytest.raises(Conflict, match="invoking"):
        journal.record_result(*args, result)


def test_result_survives_stop_without_publishing_or_releasing_budget(journal_case):
    journal, reservation, result, store = journal_case
    args = (result.build.candidate_id, reservation.reservation_id, INPUT_HASH)
    journal.begin(*args)
    journal.claims.request_stop(journal.intent_id, requested_by="operator")
    journal.record_result(*args, result)
    journal.record_result(*args, result)
    row, acquired = journal.begin(*args)
    assert not acquired and row["state"] == "result_recorded"
    assert row["result"]["build"] == result.build.model_dump(mode="json")
    with pytest.raises(Conflict, match="stop requested"):
        store.record(journal.intent_id, "builder", journal.claim_token, result)
    with journal.claims.dispatcher.repository.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM artifacts").fetchone()["n"] == 0
        assert conn.execute(
            "SELECT state FROM round_budget_reservations"
        ).fetchone()["state"] == "reserved"
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            conn.execute("UPDATE formal_build_journal SET state = 'invoking', "
                         "finished_at = NULL, result = NULL")


def test_missing_budget_wrong_owner_and_stop_prevent_invocation(journal_case):
    journal, reservation, result, _ = journal_case
    candidate = result.build.candidate_id
    with pytest.raises(Conflict, match="reserved build budget"):
        journal.begin(candidate, uuid4(), INPUT_HASH)
    intruder = PostgresFormalBuildJournal(
        journal.claims, journal.intent_id, "intruder", journal.claim_token,
    )
    with pytest.raises(Conflict, match="isolated queued Job"):
        intruder.begin(candidate, reservation.reservation_id, INPUT_HASH)
    journal.claims.request_stop(journal.intent_id, requested_by="operator")
    with pytest.raises(psycopg.errors.RaiseException, match="live claim"):
        journal.begin(candidate, reservation.reservation_id, INPUT_HASH)
    with journal.claims.dispatcher.repository.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM formal_build_journal").fetchone()["n"] == 0
        assert conn.execute("SELECT state FROM jobs").fetchone()["state"] == "queued"


def test_database_materials_reject_wrong_reservation_and_owner(journal_case):
    from hcuopt.storage.formal_build_materials import PostgresFormalBuildMaterialReader

    journal, reservation, _, _ = journal_case
    reader = PostgresFormalBuildMaterialReader(journal)
    with pytest.raises(Conflict, match="reservation"):
        reader.load(uuid4())
    intruder = PostgresFormalBuildJournal(
        journal.claims, journal.intent_id, "intruder", journal.claim_token,
    )
    with pytest.raises(Conflict, match="owner"):
        PostgresFormalBuildMaterialReader(intruder).load(reservation.reservation_id)


def test_keyboard_interrupt_marks_unknown_and_never_rebuilds(journal_case, tmp_path):
    journal, reservation, _, store = journal_case
    from hcuopt.storage.formal_build_materials import PostgresFormalBuildMaterialReader

    materials = PostgresFormalBuildMaterialReader(journal).load(reservation.reservation_id)
    member = materials["member"]
    calls = []

    def interrupted(**kwargs):
        calls.append(kwargs)
        raise KeyboardInterrupt()

    builder = SimpleNamespace(enabled=True, store_id=member.source_package_store_id,
                              store_hash=member.source_package_store_hash, build_member=interrupted)
    consumer = FormalBuildConsumer(journal, builder, store, enabled=True)
    args = dict(reservation_id=reservation.reservation_id, output_dir=tmp_path)
    with pytest.raises(KeyboardInterrupt):
        consumer.execute_current_once(**args)
    with pytest.raises(Conflict, match="uncertain"):
        consumer.execute_current_once(**args)
    assert len(calls) == 1
    with journal.claims.dispatcher.repository.connection() as conn:
        assert conn.execute("SELECT state FROM formal_build_journal").fetchone()["state"] == (
            "recovery_required"
        )


@pytest.mark.parametrize("field", ["hotspot", "hotspot_intake_hash"])
def test_build_rechecks_hotspot_before_calling_builder(journal_case, tmp_path, field):
    from hcuopt.storage.formal_build_materials import PostgresFormalBuildMaterialReader

    journal, reservation, _, store = journal_case
    materials = PostgresFormalBuildMaterialReader(journal).load(reservation.reservation_id)
    member = materials["member"]
    calls = []
    builder = SimpleNamespace(enabled=True, store_id=member.source_package_store_id,
                              store_hash=member.source_package_store_hash,
                              build_member=lambda **kwargs: calls.append(kwargs))
    consumer = FormalBuildConsumer(journal, builder, store, enabled=True)
    materials[field] = {} if field == "hotspot" else "sha256:" + "f" * 64
    with pytest.raises(Conflict, match="Hotspot"):
        consumer.execute_once(reservation_id=reservation.reservation_id,
                              output_dir=tmp_path, **materials)
    assert not calls


def test_consumer_replays_output_after_publication_failure(journal_case, tmp_path, monkeypatch):
    journal, reservation, result, store = journal_case
    repo = journal.claims.dispatcher.repository
    with repo.connection() as conn:
        round_ = repo._search_round_authority(
            conn.execute("SELECT * FROM search_rounds").fetchone()
        )
        row = conn.execute("SELECT * FROM round_candidates WHERE candidate_id = %s",
                           (result.build.candidate_id,)).fetchone()
        member = RoundCandidate.model_validate({k: row[k] for k in RoundCandidate.model_fields})
        row = conn.execute("SELECT * FROM source_snapshots WHERE kind = 'baseline'").fetchone()
        baseline = SourceSnapshot.model_validate({k: row[k] for k in SourceSnapshot.model_fields})
        hotspot = conn.execute("SELECT * FROM hotspots").fetchone()
    calls = []

    def build(**kwargs):
        calls.append(kwargs)
        return result  # Explicit fixture: validates orchestration, not actual Overlay generation.

    builder = SimpleNamespace(enabled=True, store_id=member.source_package_store_id,
                              store_hash=member.source_package_store_hash, build_member=build)
    consumer = FormalBuildConsumer(journal, builder, store, enabled=True)
    args = dict(reservation_id=reservation.reservation_id, round_authority=round_, member=member,
                baseline=baseline, hotspot=hotspot["evidence"],
                hotspot_intake_hash=hotspot["intake_hash"], output_dir=tmp_path)
    record = store.record
    finalize = repo.finalize_round_budget

    def lost_settlement_response(entry):
        finalize(entry)
        raise RuntimeError("settlement response lost")

    monkeypatch.setattr(repo, "finalize_round_budget", lost_settlement_response)
    with pytest.raises(RuntimeError, match="response lost"):
        consumer.execute_once(**args)
    monkeypatch.setattr(repo, "finalize_round_budget", finalize)

    def fail(*args):
        raise RuntimeError("injected publication outage")

    monkeypatch.setattr(store, "record", fail)
    with pytest.raises(RuntimeError, match="outage"):
        consumer.execute_once(**args)
    monkeypatch.setattr(store, "record", record)
    assert consumer.execute_once(**args) == result
    assert consumer.execute_once(**args) == result
    assert len(calls) == 1
    with repo.connection() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE job_id = %s",
                           (reservation.job_id,)).fetchone()
        assert job["state"] == "succeeded" and job["attempts"] == 1
        assert conn.execute(
            "SELECT count(*) AS n FROM job_events WHERE event_type = 'formal_build_completed'"
        ).fetchone()["n"] == 1
        rows = conn.execute(
            "SELECT * FROM round_budget_ledger WHERE entry_type = 'settle'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["actual"]["build_attempts"] == 1
        assert rows[0]["actual"]["wall_seconds"] >= 0
        assert rows[0]["actual"]["exclusive_lease_seconds"] == 0
        assert rows[0]["harness_active_seconds"] == 0


def test_consumer_unknown_failure_never_retries(journal_case, tmp_path, monkeypatch):
    journal, reservation, result, store = journal_case
    repo = journal.claims.dispatcher.repository
    with repo.connection() as conn:
        round_ = repo._search_round_authority(
            conn.execute("SELECT * FROM search_rounds").fetchone()
        )
        row = conn.execute("SELECT * FROM round_candidates WHERE candidate_id = %s",
                           (result.build.candidate_id,)).fetchone()
        member = RoundCandidate.model_validate({k: row[k] for k in RoundCandidate.model_fields})
        row = conn.execute("SELECT * FROM source_snapshots WHERE kind = 'baseline'").fetchone()
        baseline = SourceSnapshot.model_validate({k: row[k] for k in SourceSnapshot.model_fields})
        hotspot = conn.execute("SELECT * FROM hotspots").fetchone()
    calls = []

    def fail(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("unknown cleanup outcome")

    builder = SimpleNamespace(enabled=True, store_id=member.source_package_store_id,
                              store_hash=member.source_package_store_hash, build_member=fail)
    consumer = FormalBuildConsumer(journal, builder, store, enabled=True)
    args = dict(reservation_id=reservation.reservation_id, round_authority=round_, member=member,
                baseline=baseline, hotspot=hotspot["evidence"],
                hotspot_intake_hash=hotspot["intake_hash"], output_dir=tmp_path)
    with pytest.raises(RuntimeError, match="cleanup"):
        consumer.execute_once(**args)
    with pytest.raises(Conflict, match="recovery required"):
        consumer.execute_once(**args)
    assert len(calls) == 1
    with repo.connection() as conn:
        assert conn.execute(
            "SELECT state FROM round_budget_reservations"
        ).fetchone()["state"] == "reserved"


def test_usage_settlement_is_concurrent_idempotent_after_stop(journal_case):
    journal, reservation, result, _ = journal_case
    args = (result.build.candidate_id, reservation.reservation_id, INPUT_HASH)
    journal.begin(*args)
    journal.record_result(*args, result, wall_seconds=12.5)
    journal.claims.request_stop(journal.intent_id, requested_by="operator")
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(journal.settle_recorded_result, *args) for _ in range(2)]
        outcomes = [c.result() for c in calls]
    assert outcomes[0] == outcomes[1]
    assert outcomes[0]["ledger_entry"]["actual"] == BudgetUsage(
        build_attempts=1, wall_seconds=12.5,
    ).model_dump(mode="json")
    with pytest.raises(Conflict, match="invoking"):
        journal.record_result(*args, result, wall_seconds=13)
    with journal.claims.dispatcher.repository.connection() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM round_budget_ledger WHERE entry_type = 'settle'"
        ).fetchone()["n"] == 1


def test_missing_and_invalid_usage_never_become_zero_charge(journal_case):
    journal, reservation, result, _ = journal_case
    args = (result.build.candidate_id, reservation.reservation_id, INPUT_HASH)
    journal.begin(*args)
    for invalid in [float("nan"), float("inf"), -1, True]:
        with pytest.raises(ValueError, match="finite and nonnegative"):
            journal.record_result(*args, result, wall_seconds=invalid)
    journal.record_result(*args, result)  # Legacy immutable output without usage.
    with pytest.raises(Conflict, match="reconciliation required"):
        journal.settle_recorded_result(*args)
    with journal.claims.dispatcher.repository.connection() as conn:
        assert conn.execute(
            "SELECT state FROM round_budget_reservations"
        ).fetchone()["state"] == "reserved"
