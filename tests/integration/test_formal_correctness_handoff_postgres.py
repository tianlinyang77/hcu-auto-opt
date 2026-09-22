# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Storage composition fixtures, not HCU or independent numerical verification."""

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from hcuopt.api.formal_start_management import FormalStartManagement
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.contracts.v1 import ManualCorrectnessResult, WorkerRegister
from hcuopt.deployment import formal_correctness_handoff as module
from hcuopt.deployment.formal_correctness_driver import FormalCorrectnessDriver
from hcuopt.deployment.formal_runtime import FormalDeploymentRuntime
from hcuopt.domain.errors import Conflict
from hcuopt.measurement.m2_formal_receipt import M2FormalPhaseExecutionReceiptStore
from hcuopt.orchestrator.search_round import artifact_family_hash
from hcuopt.storage.formal_phase_journal import PostgresFormalPhaseJournal
from hcuopt.storage.formal_search_materials import PostgresFormalSearchBatchMaterialReader
from tests.integration.test_formal_dispatch_postgres import dispatch_case  # noqa: F401
from tests.integration.test_formal_real_builder_postgres import (
    build_member,
    plans,
    real_sources,  # noqa: F401
)
from tests.integration.test_formal_start_management_postgres import isolated_dsn  # noqa: F401

pytestmark = [pytest.mark.postgres,
              pytest.mark.skipif(not os.getenv("HCUOPT_DATABASE_URL") or os.name == "nt",
                                 reason="requires Linux PostgreSQL")]


@pytest.fixture
def handoff_case(real_sources, request, monkeypatch):  # noqa: F811
    dispatcher, intent_id = request.getfixturevalue("dispatch_case")
    dispatcher.create(intent_id)
    repo = dispatcher.repository
    runtime = FormalDeploymentRuntime(
        repo, FormalStartManagement(dispatcher.coordinator, ()), enabled=True,
    )
    claim = runtime.claims.claim(intent_id, "real-test-builder", ttl_seconds=300)
    token = claim["claim_token"]
    driver = FormalCorrectnessDriver(runtime, intent_id=intent_id,
                                     worker_id="real-test-builder", claim_token=token)
    ids = [plans.FIRST_CANDIDATE_ID, plans.SECOND_CANDIDATE_ID]
    for i, candidate in enumerate(ids):
        build_member(repo, runtime.claims, intent_id, token, real_sources, request, candidate, i)
    with repo.connection() as conn:
        row = conn.execute("SELECT * FROM search_rounds").fetchone()
        members = conn.execute("SELECT * FROM round_candidates ORDER BY ordinal").fetchall()
    driver.freeze_family(expected_artifact_family_hash=artifact_family_hash(row, members))
    plan = dispatcher.coordinator.validate_for_dispatch(
        repo.get_formal_start_intent(intent_id), repo,
    )[0].resolved_plan
    repo.register_worker(WorkerRegister(
        worker_id="handoff-fixture", worker_type="gpu", adapter_profile=row["adapter_profile"],
        capabilities={"host_id": plan.authorized_host_id,
                      "resource_id": plan.authorized_resource_id},
    ))
    entries = []
    for i, candidate in enumerate(ids):
        journal = driver.prepare(candidate_id=candidate, executor_id="handoff-fixture",
                                 wall_seconds=30)
        digest = "sha256:" + str(i + 1) * 64
        journal.begin(digest)
        result = ManualCorrectnessResult(
            candidate_id=candidate, verdict="correct" if i == 0 else "incorrect",
            protocol_version="explicit-storage-fixture", raw_evidence_uri="fixture://raw",
            raw_evidence_hash=digest, verification_artifact_uri="fixture://verification",
            verification_artifact_hash=digest, adapter_provenance=[AdapterProvenance(
                profile=row["adapter_profile"], capability="kernel_correctness",
                adapter_name="StorageFixture", adapter_version="1", implementation_kind="real",
            )], cleanup_evidence={
                "fence": {"fenced": True, "resource_id": plan.authorized_resource_id,
                          "fencing_token": journal.owner["fencing_token"]},
                "health": {"healthy": True, "quarantined": False,
                           "resource_id": plan.authorized_resource_id},
            },
        )
        journal.record_result(digest, result, wall_seconds=0.01)
        journal.finalize_recorded_result(digest)
        entries.append((journal, object()))
    # This test exercises transactions only. Real raw verification is tested separately.
    monkeypatch.setattr(driver, "_validate_adapter", lambda adapter: None)
    monkeypatch.setattr(module, "reverify_result", lambda a, r, m, p, result:
                        SimpleNamespace(verdict=result.verdict))
    return driver, entries


@pytest.mark.parametrize("fault", [
    None, "partial", "stop", "audit_failure", "bad_evidence", "binding_drift",
])
def test_atomic_handoff_and_refusals(handoff_case, monkeypatch, fault):
    driver, entries = handoff_case
    repo = driver.runtime.repository
    if fault == "partial":
        entries = entries[:1]
    elif fault == "stop":
        driver.runtime.claims.request_stop(driver.intent_id, requested_by="fixture")
    elif fault == "audit_failure":
        def fail(*args):
            raise RuntimeError("injected audit failure")
        monkeypatch.setattr(module, "_insert", fail)
    elif fault == "bad_evidence":
        def invalid(*args):
            raise Conflict("raw evidence no longer verifies")
        monkeypatch.setattr(module, "reverify_result", invalid)
    elif fault == "binding_drift":
        def drift(a, r, m, p, result):
            with repo.connection() as conn:
                conn.execute("UPDATE hotspots SET evidence = evidence || "
                             "'{\"handoff_test_changed\":true}'::jsonb")
            return SimpleNamespace(verdict=result.verdict)
        monkeypatch.setattr(module, "reverify_result", drift)
    if fault:
        with pytest.raises((Conflict, RuntimeError)):
            driver.handoff_family(entries=entries)
    else:
        report = driver.handoff_family(entries=entries)
        assert driver.handoff_family(entries=list(reversed(entries))) == report
        assert not report["hcu_started"] and not report["automatic_release_allowed"]
        with pytest.raises(Conflict, match="replay differs"):
            driver.handoff_family(entries=entries[:1])
    with repo.connection() as conn:
        assert conn.execute("SELECT state FROM search_rounds").fetchone()["state"] == (
            "correctness" if fault else "search_measuring"
        )
        states = [r["state"] for r in conn.execute(
            "SELECT state FROM round_candidates ORDER BY ordinal",
        ).fetchall()]
        assert states == (["built", "built"] if fault else
                          ["correctness_passed", "correctness_failed"])
        assert conn.execute("SELECT count(*) AS n FROM task_events WHERE event_type = "
                            "'formal_correctness_family_handoff'").fetchone()["n"] == (
                                0 if fault else 1
                            )


def test_handoff_ignores_other_rounds_on_same_task(handoff_case):
    driver, entries = handoff_case
    repo = driver.runtime.repository
    intent = repo.get_formal_start_intent(driver.intent_id)
    historical_round = str(uuid4())
    with repo.connection() as conn:
        module._insert(conn, "task_events", {
            "task_id": intent.task_id,
            "event_type": "formal_correctness_family_handoff",
            "details": {"round_id": historical_round, "historical_fixture": True},
        })
    report = driver.handoff_family(entries=entries)
    assert report["round_id"] == str(intent.round_id)
    assert driver.handoff_family(entries=list(reversed(entries))) == report
    with repo.connection() as conn:
        records = conn.execute(
            "SELECT details FROM task_events WHERE task_id = %s "
            "AND event_type = 'formal_correctness_family_handoff'",
            (intent.task_id,),
        ).fetchall()
    assert len(records) == 2
    assert {r["details"]["round_id"] for r in records} == {
        historical_round, str(intent.round_id),
    }


def test_handoff_still_rejects_duplicate_current_round_audit(handoff_case):
    driver, entries = handoff_case
    report = driver.handoff_family(entries=entries)
    repo = driver.runtime.repository
    intent = repo.get_formal_start_intent(driver.intent_id)
    with repo.connection() as conn:
        module._insert(conn, "task_events", {
            "task_id": intent.task_id,
            "event_type": "formal_correctness_family_handoff", "details": report,
        })
    with pytest.raises(Conflict, match="duplicate audit records"):
        driver.handoff_family(entries=entries)


def test_search_reader_refuses_to_close_before_each_correct_member_has_receipt(
    handoff_case, tmp_path,
):
    """A correctness handoff alone must never masquerade as performance evidence."""
    driver, entries = handoff_case
    driver.handoff_family(entries=entries)
    reader = PostgresFormalSearchBatchMaterialReader(PostgresFormalPhaseJournal(
        driver.runtime.claims, driver.intent_id, driver.worker_id, driver.claim_token,
        M2FormalPhaseExecutionReceiptStore(tmp_path / "formal-search-receipts"),
    ))
    with pytest.raises(Conflict, match="terminal Search receipt"):
        reader.load()
