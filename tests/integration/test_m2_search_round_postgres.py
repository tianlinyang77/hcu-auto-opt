# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from hcuopt.contracts.m2 import (
    BudgetUsage,
    RoundBudget,
    RoundBudgetLedgerEntry,
    RoundBudgetReservation,
    RoundCandidate,
    SearchRound,
)
from hcuopt.domain.errors import Conflict
from hcuopt.storage.repository import PostgresRepository

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - optional until dev dependencies are installed
    psycopg = None
    Jsonb = None


DATABASE_URL = os.getenv("HCUOPT_DATABASE_URL")


def _hash(value: str) -> str:
    return "sha256:" + value * 64


@unittest.skipUnless(DATABASE_URL and psycopg, "requires PostgreSQL and psycopg")
@pytest.mark.postgres
class M2SearchRoundPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        assert DATABASE_URL is not None
        assert psycopg is not None
        assert Jsonb is not None
        self.repository = PostgresRepository(DATABASE_URL)
        self.repository.migrate()
        self.connection = psycopg.connect(DATABASE_URL)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "TRUNCATE tasks, target_snapshots RESTART IDENTITY CASCADE"
            )
        self.connection.commit()
        self.authority = self._create_scripted_authority()

    def tearDown(self) -> None:
        self.connection.close()

    def _create_scripted_authority(self) -> dict[str, UUID]:
        ids = {
            "stage0_task_id": uuid4(),
            "target_snapshot_id": uuid4(),
            "stage0_run_id": uuid4(),
            "source_snapshot_id": uuid4(),
            "baseline_epoch_id": uuid4(),
            "hotspot_id": uuid4(),
        }
        now = datetime.now(timezone.utc)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO target_snapshots (
                    target_snapshot_id, target_id, target_fingerprint,
                    specification, source_path
                ) VALUES (%s, 'm2-scripted-target', 'm2-scripted-fingerprint', %s, %s)
                """,
                (ids["target_snapshot_id"], Jsonb({}), "fixture://m2-target"),
            )
            cursor.execute(
                """
                INSERT INTO tasks (
                    task_id, name, workload_id, idempotency_key, state, budget,
                    automatic_release_allowed, workflow_type, target_id,
                    target_snapshot_id, adapter_profile, stage0_authority
                ) VALUES (
                    %s, 'M2 scripted Stage 0', 'm2-scripted-workload-v1',
                    'm2-scripted-stage0-task', 'completed', '{}'::jsonb, FALSE,
                    'stage0', 'm2-scripted-target', %s, 'm2-scripted-v1', 'synthetic'
                )
                """,
                (ids["stage0_task_id"], ids["target_snapshot_id"]),
            )
            cursor.execute(
                """
                INSERT INTO stage0_runs (
                    stage0_run_id, task_id, target_snapshot_id, adapter_profile,
                    mode, state, protocol_version, idempotency_key, report,
                    created_at, finalized_at
                ) VALUES (
                    %s, %s, %s, 'm2-scripted-v1', 'dry_run', 'finalized',
                    'm2-scripted-stage0-v1', 'm2-scripted-stage0-run', %s, %s, %s
                )
                """,
                (
                    ids["stage0_run_id"],
                    ids["stage0_task_id"],
                    ids["target_snapshot_id"],
                    Jsonb({"synthetic": True}),
                    now,
                    now,
                ),
            )
            cursor.execute(
                """
                INSERT INTO source_snapshots (
                    snapshot_id, task_id, kind, repository, commit, tree_hash,
                    source_hash, worktree_uri, clean, idempotency_key,
                    adapter_provenance, synthetic, created_at
                ) VALUES (
                    %s, %s, 'baseline', 'fixture://m2-source', 'fixture-commit',
                    %s, %s, 'fixture://m2-worktree', TRUE,
                    'm2-scripted-baseline-source', %s, TRUE, %s
                )
                """,
                (
                    ids["source_snapshot_id"],
                    ids["stage0_task_id"],
                    _hash("1"),
                    _hash("2"),
                    Jsonb([{"implementation_kind": "scripted"}]),
                    now,
                ),
            )
            cursor.execute(
                """
                INSERT INTO baseline_epochs (
                    baseline_epoch_id, task_id, hardware_fingerprint,
                    software_fingerprint, workload_id, configuration_hash,
                    frozen, baseline_kind, target_snapshot_id, stage0_run_id,
                    stage0_protocol_hash, source_snapshot_id, workload_hash,
                    image_digest, adapter_profile
                ) VALUES (
                    %s, %s, 'm2-hardware', 'm2-software',
                    'm2-scripted-workload-v1', %s, TRUE, 'search_round', %s,
                    %s, %s, %s, %s, %s, 'm2-scripted-v1'
                )
                """,
                (
                    ids["baseline_epoch_id"],
                    ids["stage0_task_id"],
                    _hash("3"),
                    ids["target_snapshot_id"],
                    ids["stage0_run_id"],
                    _hash("4"),
                    ids["source_snapshot_id"],
                    _hash("5"),
                    _hash("6"),
                ),
            )
            cursor.execute(
                """
                INSERT INTO hotspots (
                    hotspot_id, task_id, baseline_epoch_id, symbol, share_ratio,
                    opportunity_score, patchability, evidence, candidate_kind,
                    actor, intake_hash, idempotency_key
                ) VALUES (
                    %s, %s, %s, 'sglang.fixture.layer_norm', 0.2, 0.5,
                    'patchable', %s, 'fixture', 'scripted-fixture', %s,
                    'm2-scripted-hotspot'
                )
                """,
                (
                    ids["hotspot_id"],
                    ids["stage0_task_id"],
                    ids["baseline_epoch_id"],
                    Jsonb({"replacement_point": "sglang.fixture.layer_norm"}),
                    _hash("7"),
                ),
            )
        self.connection.commit()
        return ids

    def _round(self, **updates) -> SearchRound:
        payload = {
            "round_id": uuid4(),
            "task_id": uuid4(),
            "idempotency_key": "m2-search-round-postgres",
            "state": "intake_open",
            "run_mode": "scripted",
            "project_mode": None,
            "target_snapshot_id": self.authority["target_snapshot_id"],
            "stage0_run_id": self.authority["stage0_run_id"],
            "stage0_protocol_hash": _hash("4"),
            "baseline_epoch_id": self.authority["baseline_epoch_id"],
            "hotspot_id": self.authority["hotspot_id"],
            "replacement_point": "sglang.fixture.layer_norm",
            "workload_id": "m2-scripted-workload-v1",
            "workload_hash": _hash("5"),
            "configuration_hash": _hash("3"),
            "image_digest": _hash("6"),
            "adapter_profile": "m2-scripted-v1",
            "declared_candidate_count": 2,
            "max_promoted": 2,
            "family_alpha": 0.05,
            "search_plan_hash": _hash("8"),
            "holdout_plan_commitment": _hash("9"),
            "holdout_plan_authority_id": "synthetic-holdout-v1",
            "holdout_plan_authority_hash": _hash("a"),
            "selection_rule_hash": _hash("b"),
            "budget": RoundBudget(
                max_candidates=2,
                max_build_attempts=4,
                max_correctness_attempts=4,
                max_search_samples=200,
                max_holdout_samples=200,
                max_wall_seconds=600,
                max_exclusive_lease_seconds=300,
            ),
            "version": 1,
            "created_at": datetime.now(timezone.utc),
        }
        payload.update(updates)
        return SearchRound.model_validate(payload)

    def _candidate(self, round_id: UUID, ordinal: int, **updates) -> RoundCandidate:
        payload = {
            "round_candidate_id": uuid4(),
            "round_id": round_id,
            "candidate_id": uuid4(),
            "ordinal": ordinal,
            "source_package_store_id": f"synthetic-store-{ordinal}",
            "source_package_store_hash": _hash(str(ordinal + 1)),
            "source_package_hash": _hash(str(ordinal + 2)),
            "source_manifest_hash": _hash(str(ordinal + 3)),
            "baseline_source_hash": _hash("2"),
            "candidate_source_hash": _hash(str(ordinal + 7)),
            "optimization_intent": f"known signal {ordinal}",
            "replacement_point": "sglang.fixture.layer_norm",
            "candidate_kind": "fixture",
            "state": "intake_accepted",
            "idempotency_key": f"m2-round-candidate-{ordinal}",
        }
        payload.update(updates)
        return RoundCandidate.model_validate(payload)

    def _add_job(self, task_id: UUID, suffix: str) -> UUID:
        job_id = uuid4()
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO jobs (
                    job_id, task_id, job_type, accepted_worker_type,
                    lease_scope, payload, idempotency_key
                ) VALUES (%s, %s, 'manual_performance', 'gpu', 'exclusive', %s, %s)
                """,
                (job_id, task_id, Jsonb({"fixture": suffix}), f"m2-job-{suffix}"),
            )
        self.connection.commit()
        return job_id

    def _reservation(
        self,
        round_id: UUID,
        job_id: UUID,
        suffix: str,
        planned: BudgetUsage,
    ) -> tuple[RoundBudgetReservation, RoundBudgetLedgerEntry]:
        reservation = RoundBudgetReservation(
            reservation_id=uuid4(),
            round_id=round_id,
            job_id=job_id,
            attempt=1,
            phase="search",
            planned=planned,
            state="reserved",
            idempotency_key=f"m2-reservation-{suffix}",
        )
        entry = RoundBudgetLedgerEntry(
            ledger_entry_id=uuid4(),
            reservation_id=reservation.reservation_id,
            round_id=round_id,
            entry_type="reserve",
            reserved=planned,
            actual=BudgetUsage(),
            lease_held_seconds=0,
            harness_active_seconds=0,
            raw_usage_evidence_hash=_hash("d"),
            idempotency_key=f"m2-ledger-reserve-{suffix}",
            created_at=datetime.now(timezone.utc),
        )
        return reservation, entry

    def test_round_create_is_idempotent_and_rejects_different_inputs(self) -> None:
        request = self._round()
        first = self.repository.create_search_round(request)
        replay = self.repository.create_search_round(request)

        self.assertEqual(first["round_id"], replay["round_id"])
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM search_rounds")
            self.assertEqual(cursor.fetchone()[0], 1)
            cursor.execute(
                "SELECT count(*) FROM task_events WHERE event_type = 'm2_round_created'"
            )
            self.assertEqual(cursor.fetchone()[0], 1)

        with self.assertRaises(Conflict):
            self.repository.create_search_round(
                request.model_copy(update={"search_plan_hash": _hash("c")})
            )

    def test_candidate_intake_and_concurrent_close_freeze_one_family(self) -> None:
        request = self._round()
        self.repository.create_search_round(request)
        with self.assertRaises(Conflict):
            self.repository.close_search_round_intake(request.round_id)

        first = self._candidate(request.round_id, 0)
        second = self._candidate(request.round_id, 1)
        self.repository.add_round_candidate(first)
        replay = self.repository.add_round_candidate(first)
        self.assertEqual(replay["round_candidate_id"], first.round_candidate_id)
        self.repository.add_round_candidate(second)

        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(
                pool.map(
                    lambda _: self.repository.close_search_round_intake(request.round_id),
                    range(2),
                )
            )
        self.assertEqual(rows[0]["candidate_family_hash"], rows[1]["candidate_family_hash"])
        self.assertEqual(rows[0]["state"], "intake_closed")
        self.assertEqual(rows[0]["version"], 2)
        self.assertEqual(
            self.repository.create_search_round(request)["candidate_family_hash"],
            rows[0]["candidate_family_hash"],
        )
        self.assertEqual(
            self.repository.add_round_candidate(first)["round_candidate_id"],
            first.round_candidate_id,
        )

        summary = self.repository.search_round_summary(request.round_id)
        self.assertEqual(len(summary["candidates"]), 2)
        self.assertFalse(summary["automatic_release_allowed"])
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM task_events WHERE event_type = 'm2_round_intake_closed'"
            )
            self.assertEqual(cursor.fetchone()[0], 1)

        with self.assertRaises(Conflict):
            self.repository.add_round_candidate(
                self._candidate(request.round_id, 1, idempotency_key="new-after-close")
            )

    def test_budget_reservation_is_atomic_and_settle_records_overrun(self) -> None:
        request = self._round()
        self.repository.create_search_round(request)
        self.repository.add_round_candidate(self._candidate(request.round_id, 0))
        self.repository.add_round_candidate(self._candidate(request.round_id, 1))
        self.repository.close_search_round_intake(request.round_id)

        first_job = self._add_job(request.task_id, "budget-first")
        second_job = self._add_job(request.task_id, "budget-second")
        planned = BudgetUsage(
            search_samples=150,
            wall_seconds=20,
            exclusive_lease_seconds=10,
        )
        attempts = [
            self._reservation(request.round_id, first_job, "first", planned),
            self._reservation(request.round_id, second_job, "second", planned),
        ]

        def reserve(pair):
            try:
                return "reserved", self.repository.reserve_round_budget(*pair)
            except Conflict:
                return "conflict", None

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(reserve, attempts))
        self.assertEqual([item[0] for item in outcomes].count("reserved"), 1)
        self.assertEqual([item[0] for item in outcomes].count("conflict"), 1)

        winner_index = next(index for index, item in enumerate(outcomes) if item[0] == "reserved")
        reservation, reserve_entry = attempts[winner_index]
        replay = self.repository.reserve_round_budget(reservation, reserve_entry)
        self.assertEqual(replay["reservation"]["reservation_id"], reservation.reservation_id)

        settle = RoundBudgetLedgerEntry(
            ledger_entry_id=uuid4(),
            reservation_id=reservation.reservation_id,
            round_id=request.round_id,
            entry_type="settle",
            reserved=planned,
            actual=BudgetUsage(
                search_samples=220,
                wall_seconds=25,
                exclusive_lease_seconds=12,
            ),
            lease_held_seconds=12,
            harness_active_seconds=8,
            raw_usage_evidence_hash=_hash("e"),
            idempotency_key="m2-ledger-settle-overrun",
            created_at=datetime.now(timezone.utc),
        )
        terminal = self.repository.finalize_round_budget(settle)
        self.assertEqual(terminal["reservation"]["state"], "settled")
        replay_terminal = self.repository.finalize_round_budget(settle)
        self.assertEqual(
            replay_terminal["ledger_entry"]["ledger_entry_id"],
            settle.ledger_entry_id,
        )

        third_job = self._add_job(request.task_id, "budget-after-overrun")
        third = self._reservation(
            request.round_id,
            third_job,
            "after-overrun",
            BudgetUsage(search_samples=1),
        )
        with self.assertRaises(Conflict):
            self.repository.reserve_round_budget(*third)

        release = settle.model_copy(
            update={
                "ledger_entry_id": uuid4(),
                "entry_type": "release",
                "actual": BudgetUsage(),
                "lease_held_seconds": 0,
                "harness_active_seconds": 0,
                "idempotency_key": "m2-ledger-release-too-late",
                "created_at": datetime.now(timezone.utc),
            }
        )
        with self.assertRaises(Conflict):
            self.repository.finalize_round_budget(release)
