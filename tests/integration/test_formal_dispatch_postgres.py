# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Real transactions and authority rows; signatures and source inputs are fixtures."""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from hcuopt.contracts.m2_formal_start_v1 import FormalStartIntentRequest
from hcuopt.domain.errors import Conflict
from hcuopt.storage import formal_dispatch as dispatch_module
from hcuopt.storage.formal_dispatch import PostgresFormalDispatcher, _insert
from hcuopt.storage.repository import PostgresRepository
from tests.integration.test_formal_start_management_postgres import isolated_dsn  # noqa: F401
from tests.unit import test_formal_operator_plans as plans
from tests.unit import test_formal_operator_start as starts
from tests.unit.test_formal_start_management_api import setup_management

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.getenv("HCUOPT_DATABASE_URL"), reason="requires PostgreSQL"),
]


def seed_authority(repository, coordinator, memory):  # type: ignore[no-untyped-def]
    snapshot = memory.authority.snapshot
    authority, hotspot = snapshot.authority, snapshot.hotspot
    plan = coordinator.object_store.preview.resolved_plan
    target = coordinator.compiler.profiles.require(
        plan.target_profile, plan.run_mode
    ).authority_refs
    stage_task, baseline_task, source_id = uuid4(), uuid4(), uuid4()
    now = plans.NOW
    with repository.connection() as connection:
        _insert(
            connection,
            "target_snapshots",
            {
                "target_snapshot_id": authority.target_snapshot_id,
                "target_id": target.target_id,
                "target_fingerprint": target.target_spec_hash,
                "specification": {},
                "source_path": "fixture://formal-dispatch-target",
            },
        )
        for task_id, workflow in ((stage_task, "stage0"), (baseline_task, "walking_skeleton")):
            _insert(
                connection,
                "tasks",
                {
                    "task_id": task_id,
                    "name": "dispatch-test-parent",
                    "workload_id": authority.workload_id,
                    "idempotency_key": str(task_id),
                    "state": "completed",
                    "budget": {},
                    "workflow_type": workflow,
                    "target_id": target.target_id,
                    "target_snapshot_id": authority.target_snapshot_id,
                    "adapter_profile": authority.adapter_profile,
                    "stage0_authority": "formal",
                    "project_mode": "degraded_manual_intake",
                },
            )
        _insert(
            connection,
            "stage0_runs",
            {
                "stage0_run_id": authority.stage0_run_id,
                "task_id": stage_task,
                "target_snapshot_id": authority.target_snapshot_id,
                "adapter_profile": authority.adapter_profile,
                "mode": "formal",
                "state": "finalized",
                "protocol_version": "dispatch-test-v1",
                "idempotency_key": str(authority.stage0_run_id),
                "report": {},
                "created_at": now,
                "finalized_at": now,
            },
        )
        _insert(
            connection,
            "stage0_evidence",
            {
                "task_id": stage_task,
                "stage0_run_id": authority.stage0_run_id,
                "evidence": {
                    "synthetic": False,
                    "stage0_run_id": str(authority.stage0_run_id),
                    "protocol_version": "dispatch-test-v1",
                    "protocol_hash": authority.stage0_protocol_hash,
                },
                "report": {"evidence_authority": "formal", "automatic_release_allowed": False},
            },
        )
        _insert(
            connection,
            "source_snapshots",
            {
                "snapshot_id": source_id,
                "task_id": baseline_task,
                "kind": "baseline",
                "repository": "fixture://source",
                "commit": "a" * 40,
                "tree_hash": authority.baseline_source_hash,
                "source_hash": authority.baseline_source_hash,
                "worktree_uri": "fixture://worktree",
                "clean": True,
                "idempotency_key": str(source_id),
                "adapter_provenance": Jsonb([{"implementation_kind": "git"}]),
                "synthetic": False,
                "created_at": now,
            },
        )
        _insert(
            connection,
            "baseline_epochs",
            {
                "baseline_epoch_id": authority.baseline_epoch_id,
                "task_id": baseline_task,
                "hardware_fingerprint": "test-hardware",
                "software_fingerprint": "test-software",
                "workload_id": authority.workload_id,
                "configuration_hash": authority.configuration_hash,
                "frozen": True,
                "baseline_kind": "manual_candidate",
                "target_snapshot_id": authority.target_snapshot_id,
                "stage0_run_id": authority.stage0_run_id,
                "stage0_protocol_hash": authority.stage0_protocol_hash,
                "source_snapshot_id": source_id,
                "workload_hash": authority.workload_hash,
                "image_digest": authority.image_digest,
                "adapter_profile": authority.adapter_profile,
            },
        )
        _insert(
            connection,
            "hotspots",
            {
                "hotspot_id": authority.hotspot_id,
                "task_id": baseline_task,
                "baseline_epoch_id": authority.baseline_epoch_id,
                "symbol": authority.replacement_point,
                "share_ratio": 0.1,
                "opportunity_score": 0.1,
                "patchability": "overlay",
                "candidate_kind": "business",
                "intake_hash": hotspot.hotspot_intake_hash,
                "evidence": {
                    "replacement_point": hotspot.replacement_point,
                    "profiler_raw_output_uri": hotspot.profiler_evidence_uri,
                    "profiler_raw_output_hash": hotspot.profiler_evidence_hash,
                    "correctness_spec_uri": hotspot.correctness_evidence_uri,
                    "correctness_spec_hash": hotspot.correctness_evidence_hash,
                    "shape": list(hotspot.shape),
                    "dtype": hotspot.dtype,
                },
            },
        )


@pytest.fixture
def dispatch_case(isolated_dsn, tmp_path, monkeypatch):  # type: ignore[no-untyped-def] # noqa: F811
    repository = PostgresRepository(isolated_dsn)
    with repository.connection() as connection:
        now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    # Move only test authority clocks; the production dispatcher uses DB time.
    monkeypatch.setattr(plans, "NOW", now)
    monkeypatch.setattr(plans, "WINDOW_START", now - timedelta(minutes=5))
    monkeypatch.setattr(plans, "WINDOW_END", now + timedelta(hours=1))
    monkeypatch.setattr(starts, "NOW", now)
    management, memory, payload = setup_management(tmp_path)
    coordinator = management.coordinator
    seed_authority(repository, coordinator, memory)
    request = FormalStartIntentRequest(
        **payload, actor_assertion=management.capabilities[0].assertion
    )
    result = coordinator.create(request, repository)
    assert result.state == "ready_for_round_creation"
    return PostgresFormalDispatcher(repository, coordinator, enabled=True), result.intent_id


def test_atomic_concurrent_creation_replay_and_cancel(dispatch_case):  # type: ignore[no-untyped-def]
    dispatcher, intent_id = dispatch_case
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: dispatcher.create(intent_id), range(2)))
    assert {result["replayed"] for result in results} == {False, True}
    assert results[0]["round_id"] == results[1]["round_id"]
    repository = dispatcher.repository
    with repository.connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM search_rounds").fetchone()["n"] == 1
        assert connection.execute("SELECT count(*) AS n FROM round_candidates").fetchone()["n"] == 2
        assert connection.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0
    fresh = PostgresFormalDispatcher(
        PostgresRepository(repository.database_url),
        dispatcher.coordinator,
        enabled=True,
    )
    assert fresh.create(intent_id)["replayed"]
    before = repository.get_formal_start_intent(intent_id)
    after = repository.record_formal_start_reconciliation(
        intent_id,
        state="awaiting_authority",
        blocker_codes=("expired",),
        checked_at=plans.NOW,
    )
    assert before == after
    with repository.connection() as connection:
        cancelled_at = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    repository.cancel_formal_start_intent(intent_id, cancelled_at=cancelled_at)
    assert fresh.create(intent_id)["state"] == "cancelled"
    assert repository.get_search_round(before.round_id)["state"] == "cancelled"


def test_disabled_and_cancel_before_create(dispatch_case):  # type: ignore[no-untyped-def]
    dispatcher, intent_id = dispatch_case
    with pytest.raises(Conflict, match="disabled"):
        PostgresFormalDispatcher(dispatcher.repository, dispatcher.coordinator).create(intent_id)
    dispatcher.repository.cancel_formal_start_intent(intent_id, cancelled_at=plans.NOW)
    with pytest.raises(Conflict):
        dispatcher.create(intent_id)


def test_rollback_after_full_intake_before_outbox(dispatch_case, monkeypatch):  # type: ignore[no-untyped-def]
    dispatcher, intent_id = dispatch_case
    original = dispatcher._write_round

    def fail(connection, prepared):  # type: ignore[no-untyped-def]
        original(connection, prepared)
        raise RuntimeError("injected transaction fault")

    monkeypatch.setattr(dispatcher, "_write_round", fail)
    with pytest.raises(RuntimeError, match="injected"):
        dispatcher.create(intent_id)
    with dispatcher.repository.connection() as connection:
        for table in ("search_rounds", "candidates", "round_candidates", "formal_round_dispatches"):
            assert (
                connection.execute(
                    psycopg.sql.SQL("SELECT count(*) AS n FROM {}").format(
                        psycopg.sql.Identifier(table)
                    )
                ).fetchone()["n"]
                == 0
            )
    monkeypatch.setattr(dispatcher, "_write_round", original)
    assert dispatcher.create(intent_id)["state"] == "queued"


def test_locked_window_and_intent_version_are_rechecked(dispatch_case, monkeypatch):  # type: ignore[no-untyped-def]
    dispatcher, intent_id = dispatch_case
    original = dispatch_module.prepare_formal_round

    def expired(*args):  # type: ignore[no-untyped-def]
        prepared = original(*args)
        return replace(prepared, valid_until=plans.NOW - timedelta(seconds=1))

    monkeypatch.setattr(dispatch_module, "prepare_formal_round", expired)
    with pytest.raises(Conflict, match="window"):
        dispatcher.create(intent_id)

    def version_changed(*args):  # type: ignore[no-untyped-def]
        prepared = original(*args)
        dispatcher.repository.record_formal_start_reconciliation(
            intent_id,
            state="ready_for_round_creation",
            blocker_codes=(),
            checked_at=plans.NOW,
        )
        return prepared

    monkeypatch.setattr(dispatch_module, "prepare_formal_round", version_changed)
    with pytest.raises(Conflict, match="changed"):
        dispatcher.create(intent_id)
    with dispatcher.repository.connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM search_rounds").fetchone()["n"] == 0


def test_dispatch_identity_is_frozen_and_scripted_cannot_run_it(dispatch_case):  # type: ignore[no-untyped-def]
    dispatcher, intent_id = dispatch_case
    result = dispatcher.create(intent_id)
    with pytest.raises(Conflict):
        dispatcher.repository.reconcile_scripted_search_round(result["round_id"])
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        with dispatcher.repository.connection() as connection:
            connection.execute(
                "UPDATE formal_round_dispatches SET resolved_plan_hash = %s WHERE intent_id = %s",
                ("sha256:" + "f" * 64, intent_id),
            )
    with pytest.raises(psycopg.errors.RaiseException, match="dispatch-aware"):
        with dispatcher.repository.connection() as connection:
            connection.execute(
                "UPDATE formal_operator_start_intents SET version = version + 1 "
                "WHERE intent_id = %s",
                (intent_id,),
            )
