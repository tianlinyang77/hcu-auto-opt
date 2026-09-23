# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

from hcuopt.contracts.m2_formal_execution_v1 import (
    M2FormalPhaseExecutionRecord,
    M2FormalPhaseExecutionRequest,
    m2_formal_execution_id_for,
    m2_formal_phase_execution_request_hash,
)
from hcuopt.domain.enums import RoundPhase
from hcuopt.domain.errors import Conflict
from hcuopt.measurement.m2_formal_receipt import M2FormalPhaseExecutionReceiptStore
from hcuopt.storage.formal_claim import PostgresFormalClaimStore
from hcuopt.storage.formal_phase_journal import PostgresFormalPhaseJournal
from hcuopt.workers.formal_phase_consumer import FormalPhaseConsumer
from tests.integration.test_formal_dispatch_postgres import dispatch_case  # noqa: F401
from tests.integration.test_formal_start_management_postgres import isolated_dsn  # noqa: F401
from tests.unit import test_m2_formal_execution as fixture

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.getenv("HCUOPT_DATABASE_URL"), reason="requires PostgreSQL"),
]


@pytest.fixture
def journal_case(dispatch_case, tmp_path):  # type: ignore[no-untyped-def] # noqa: F811
    dispatcher, intent_id = dispatch_case
    dispatch = dispatcher.create(intent_id)
    claims = PostgresFormalClaimStore(dispatcher, enabled=True)
    claim = claims.claim(intent_id, "worker", ttl_seconds=300)
    journal = PostgresFormalPhaseJournal(
        claims,
        intent_id,
        "worker",
        claim["claim_token"],
        M2FormalPhaseExecutionReceiptStore(tmp_path / "receipts"),
    )
    round_ = fixture._round(RoundPhase.SEARCH)
    authority = fixture._authority(round_)
    member = fixture._member(round_, RoundPhase.SEARCH)
    template = fixture._request(round_, authority, member, RoundPhase.SEARCH)
    intent = dispatcher.repository.get_formal_start_intent(intent_id)
    binding = intent.candidate_bindings[0]
    common = dict(
        round_id=intent.round_id,
        candidate_id=binding.candidate_id,
        round_candidate_id=binding.round_candidate_id,
    )
    request = M2FormalPhaseExecutionRequest.model_validate(
        {
            **template.model_dump(mode="python"),
            "binding": {
                **template.binding.model_dump(mode="python"),
                **common,
                "task_id": intent.task_id,
                "resolved_plan_hash": intent.resolved_plan_hash,
                "formal_authorization_hash": intent.formal_authorization_hash,
                "candidate_family_hash": dispatch["candidate_family_hash"],
            },
            "reservation": {**template.reservation.model_dump(mode="python"), **common},
        }
    )
    return journal, request


def test_unique_invocation_survives_reconstruction_and_changed_attempt(journal_case):  # type: ignore[no-untyped-def]
    journal, request = journal_case
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: journal.begin(request), range(2)))
    assert sorted(created for _, created in results) == [False, True]
    fresh = PostgresFormalPhaseJournal(
        journal.claims,
        journal.intent_id,
        journal.worker_id,
        journal.claim_token,
        journal.receipt_store,
    )
    row, acquired = fresh.begin(request)
    assert not acquired and row["state"] == "invoking"
    changed = request.model_copy(
        update={
            "binding": request.binding.model_copy(update={"attempt": 2}),
            "reservation": request.reservation.model_copy(update={"attempt": 2}),
        }
    )
    with pytest.raises(Conflict, match="another request"):
        fresh.begin(changed)
    fresh.mark_unknown(request)
    assert fresh.begin(request)[0]["state"] == "recovery_required"
    with journal.claims.dispatcher.repository.connection() as connection:
        assert (
            connection.execute("SELECT count(*) AS n FROM formal_phase_journal_events").fetchone()[
                "n"
            ]
            == 2
        )
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            connection.execute(
                "UPDATE formal_phase_journal SET state = 'invoking', finished_at = NULL"
            )


def failure_receipt(journal, request):  # type: ignore[no-untyped-def]
    # Explicit fixture: no real execution is claimed by this database test.
    record = M2FormalPhaseExecutionRecord(
        execution_id=m2_formal_execution_id_for(request.binding),
        request_hash=m2_formal_phase_execution_request_hash(request),
        binding=request.binding,
        target_lock_refresh=fixture._refresh(request.binding),
        adapter_provenance=fixture.AdapterProvenance(
            profile=request.binding.adapter_profile,
            capability="formal_execution_adapter",
            adapter_name="test-fixture",
            adapter_version=request.binding.adapter_profile_version,
            implementation_kind="real",
        ),
        status="failed",
        started_at=fixture.NOW,
        finished_at=fixture.NOW,
        actual=fixture.BudgetUsage(),
        lease_held_seconds=0,
        harness_active_seconds=0,
        cleanup_evidence=fixture._cleanup(request.binding),
        cleanup_status="verified",
        termination_reason="fixture failure",
        error_code="fixture_failure",
    )
    return journal.receipt_store.publish(record)


def test_receipt_replay_and_late_completion_rejected(journal_case):  # type: ignore[no-untyped-def]
    journal, request = journal_case
    journal.begin(request)
    reference = failure_receipt(journal, request)
    journal.record_receipt(request, reference)
    journal.record_receipt(request, reference)
    journal.mark_unknown(request)
    row, acquired = journal.begin(request)
    assert not acquired and row["state"] == "receipt_recorded"
    assert row["receipt_ref"] == reference.model_dump(mode="json")


def test_unknown_cannot_be_completed_or_reowned(journal_case):  # type: ignore[no-untyped-def]
    journal, request = journal_case
    journal.begin(request)
    journal.mark_unknown(request)
    reference = failure_receipt(journal, request)
    with pytest.raises(Conflict, match="late completion"):
        journal.record_receipt(request, reference)
    wrong = PostgresFormalPhaseJournal(
        journal.claims, journal.intent_id, "other", uuid4(), journal.receipt_store
    )
    with pytest.raises(Conflict, match="another request or owner"):
        wrong.begin(request)


def test_stop_before_begin_and_transaction_failure_leave_no_invocation(journal_case, monkeypatch):  # type: ignore[no-untyped-def]
    from hcuopt.storage import formal_phase_journal

    journal, request = journal_case
    original = formal_phase_journal._insert

    def crash(connection, table, payload):  # type: ignore[no-untyped-def]
        original(connection, table, payload)
        raise RuntimeError("injected before commit")

    monkeypatch.setattr(formal_phase_journal, "_insert", crash)
    with pytest.raises(RuntimeError, match="injected"):
        journal.begin(request)
    monkeypatch.setattr(formal_phase_journal, "_insert", original)
    journal.claims.request_stop(journal.intent_id, requested_by="operator")
    with pytest.raises(psycopg.errors.RaiseException, match="unstopped"):
        journal.begin(request)
    with journal.claims.dispatcher.repository.connection() as connection:
        assert (
            connection.execute("SELECT count(*) AS n FROM formal_phase_journal").fetchone()["n"]
            == 0
        )


def test_consumer_replay_uses_durable_receipt_without_second_callback(journal_case, tmp_path):  # type: ignore[no-untyped-def]
    journal, request = journal_case
    calls = []

    class FixtureAdapter:
        def run(self, **kwargs):  # type: ignore[no-untyped-def]
            kwargs["execution_checkpoint"](kwargs["request"])
            calls.append(1)
            return SimpleNamespace(receipt_ref=failure_receipt(journal, request))

    args = dict(
        round_authority=None,
        formal_authority=None,
        member=None,
        request=request,
        output_dir=tmp_path,
    )
    first = FormalPhaseConsumer(journal, FixtureAdapter(), enabled=True).execute_once(**args)
    fresh = PostgresFormalPhaseJournal(
        journal.claims,
        journal.intent_id,
        journal.worker_id,
        journal.claim_token,
        journal.receipt_store,
    )
    second = FormalPhaseConsumer(fresh, FixtureAdapter(), enabled=True).execute_once(**args)
    assert first == second
    assert first.execution.status == "failed"
    assert calls == [1]
