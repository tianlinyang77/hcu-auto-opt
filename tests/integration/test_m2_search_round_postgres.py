# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from hcuopt.contracts.m2 import (
    ArtifactFamilyFreezeRequest,
    BudgetUsage,
    RoundBudget,
    RoundBudgetLedgerEntry,
    RoundBudgetReservation,
    RoundCandidate,
    RoundCandidateBuildTerminal,
    SearchRound,
)
from hcuopt.domain.enums import RoundCandidateState, SearchRoundState
from hcuopt.domain.errors import Conflict
from hcuopt.evaluation.m2_authority import SyntheticHoldoutPlanAuthority
from hcuopt.evaluation.m2_finalizer import M2ScriptedRoundFinalizer
from hcuopt.evaluation.m2_models import BarrierMemberResult
from hcuopt.evaluation.m2_statistics import (
    ScriptedCandidateStatisticsInput,
    SearchBarrierDecision,
    bonferroni_fwer,
    close_scripted_holdout_barrier,
    close_scripted_search_barrier,
)
from hcuopt.evaluation.m2_verifier import (
    SyntheticEvidenceStore,
    build_scripted_round_evidence,
    publish_scripted_evidence_index,
    scripted_round_evidence_requirements,
)
from hcuopt.orchestrator.search_round import (
    artifact_family_hash,
    round_budget_ledger_document,
)
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

    def _record_synthetic_artifact(
        self,
        task_id: UUID,
        candidate_id: UUID,
        content_hash: str,
        *,
        synthetic: bool = True,
    ) -> UUID:
        artifact_id = uuid4()
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, task_id, candidate_id, kind, uri,
                    content_hash, metadata, synthetic
                ) VALUES (%s, %s, %s, 'scripted_overlay', %s, %s, '{}'::jsonb, %s)
                """,
                (
                    artifact_id,
                    task_id,
                    candidate_id,
                    f"fixture:///m2/artifacts/{artifact_id}",
                    content_hash,
                    synthetic,
                ),
            )
        self.connection.commit()
        return artifact_id

    @staticmethod
    def _round_authority(row: dict) -> SearchRound:
        return SearchRound.model_validate(
            {name: row[name] for name in SearchRound.model_fields}
        )

    @staticmethod
    def _publish(store: SyntheticEvidenceStore, label: str):
        return store.publish({"label": label, "synthetic": True})

    def _prepare_scripted_evaluation(
        self,
        *,
        promote: bool,
    ) -> tuple[
        SyntheticEvidenceStore,
        SyntheticHoldoutPlanAuthority,
        SearchRound,
        tuple[BarrierMemberResult, ...],
        SearchBarrierDecision,
    ]:
        now = datetime.now(timezone.utc)
        store = SyntheticEvidenceStore()
        holdout_authority = SyntheticHoldoutPlanAuthority(
            authority_id=f"m2-scripted-holdout-{uuid4()}",
            authority_version="1.0.0",
        )
        plan = {
            "protocol_version": "m2-scripted-holdout-v1",
            "cases": [{"shape": [1, 2048], "dtype": "float16", "seed": 20260827}],
        }
        request_id = uuid4()
        commitment = holdout_authority.commit_plan(
            round_id=request_id,
            plan=plan,
            nonce=bytes(range(32)),
            created_at=now,
        )
        request = self._round(
            round_id=request_id,
            task_id=uuid4(),
            idempotency_key=f"m2-scripted-finalizer-{request_id}",
            search_plan_hash=self._publish(store, f"search-plan-{request_id}").sha256,
            holdout_plan_commitment=commitment.commitment,
            holdout_plan_authority_id=holdout_authority.authority_id,
            holdout_plan_authority_hash=holdout_authority.authority_hash,
            selection_rule_hash=self._publish(
                store, f"selection-rule-{request_id}"
            ).sha256,
        )
        self.repository.create_search_round(request)
        candidates = tuple(
            self._candidate(
                request.round_id,
                ordinal,
                idempotency_key=f"m2-finalizer-candidate-{request_id}-{ordinal}",
            )
            for ordinal in range(2)
        )
        for candidate in candidates:
            self.repository.add_round_candidate(candidate)
        closed = self.repository.close_search_round_intake(request.round_id)

        search_members: list[BarrierMemberResult] = []
        statistics: list[ScriptedCandidateStatisticsInput] = []
        for ordinal, candidate in enumerate(candidates):
            artifact = self._publish(store, f"artifact-{request_id}-{ordinal}")
            artifact_id = self._record_synthetic_artifact(
                request.task_id,
                candidate.candidate_id,
                artifact.sha256,
            )
            self.repository.record_round_candidate_build(
                RoundCandidateBuildTerminal(
                    round_id=request.round_id,
                    round_candidate_id=candidate.round_candidate_id,
                    candidate_id=candidate.candidate_id,
                    state="built",
                    artifact_id=artifact_id,
                    artifact_hash=artifact.sha256,
                )
            )
            correctness = self._publish(
                store, f"correctness-search-{request_id}-{ordinal}"
            )
            member = BarrierMemberResult(
                round_candidate_id=candidate.round_candidate_id,
                candidate_id=candidate.candidate_id,
                candidate_state=RoundCandidateState.SEARCH_MEASURED,
                artifact_id=artifact_id,
                artifact_hash=artifact.sha256,
                correctness_evidence_hash=correctness.sha256,
                scripted_phase_receipt_id=uuid4(),
                budget_usage_evidence_hash=self._publish(
                    store, f"budget-search-{request_id}-{ordinal}"
                ).sha256,
                cleanup_evidence_hash=self._publish(
                    store, f"cleanup-search-{request_id}-{ordinal}"
                ).sha256,
                synthetic=True,
            )
            search_members.append(member)
            effect = 0.15 + ordinal * 0.02 if promote else -0.10 - ordinal * 0.02
            statistics.append(
                ScriptedCandidateStatisticsInput(
                    candidate_id=candidate.candidate_id,
                    scripted_phase_receipt_id=member.scripted_phase_receipt_id,
                    correctness_evidence_hash=correctness.sha256,
                    raw_evidence_hash=self._publish(
                        store, f"raw-search-{request_id}-{ordinal}"
                    ).sha256,
                    baseline_sample_set_hash=self._publish(
                        store, f"baseline-search-{request_id}-{ordinal}"
                    ).sha256,
                    restart_effects=(effect,) * 4,
                    baseline_restart_means_ns=(100.0,) * 4,
                    stage0_mde_ratio=0.03,
                )
            )

        frozen_summary = self.repository.search_round_summary(request.round_id)
        artifact_hash = artifact_family_hash(
            frozen_summary["round"], frozen_summary["candidates"]
        )
        self.repository.freeze_search_round_artifact_family(
            ArtifactFamilyFreezeRequest(
                round_id=request.round_id,
                candidate_family_hash=closed["candidate_family_hash"],
                expected_artifact_family_hash=artifact_hash,
            )
        )
        search_row = self.repository.get_search_round(request.round_id)
        search_authority = self._round_authority(search_row).model_copy(
            update={"state": SearchRoundState.SEARCH_BARRIER}
        )
        decision = close_scripted_search_barrier(
            round_authority=search_authority,
            expected_candidate_ids=(item.candidate_id for item in search_members),
            members=tuple(search_members),
            statistics=tuple(statistics),
            closed_by="m2-scripted-evaluation-authority",
            closed_at=now,
            idempotency_key=f"m2-search-barrier-{request_id}",
        )
        return store, holdout_authority, request, tuple(search_members), decision

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

    def test_build_terminals_and_concurrent_artifact_family_freeze(self) -> None:
        request = self._round()
        self.repository.create_search_round(request)
        first = self._candidate(request.round_id, 0)
        second = self._candidate(request.round_id, 1)
        self.repository.add_round_candidate(first)
        self.repository.add_round_candidate(second)
        closed = self.repository.close_search_round_intake(request.round_id)

        artifact_hash = _hash("f")
        untrusted_artifact_id = self._record_synthetic_artifact(
            request.task_id,
            first.candidate_id,
            _hash("e"),
            synthetic=False,
        )
        with self.assertRaises(Conflict):
            self.repository.record_round_candidate_build(
                RoundCandidateBuildTerminal(
                    round_id=request.round_id,
                    round_candidate_id=first.round_candidate_id,
                    candidate_id=first.candidate_id,
                    state="built",
                    artifact_id=untrusted_artifact_id,
                    artifact_hash=_hash("e"),
                )
            )
        artifact_id = self._record_synthetic_artifact(
            request.task_id, first.candidate_id, artifact_hash
        )
        built = RoundCandidateBuildTerminal(
            round_id=request.round_id,
            round_candidate_id=first.round_candidate_id,
            candidate_id=first.candidate_id,
            state="built",
            artifact_id=artifact_id,
            artifact_hash=artifact_hash,
        )
        failed = RoundCandidateBuildTerminal(
            round_id=request.round_id,
            round_candidate_id=second.round_candidate_id,
            candidate_id=second.candidate_id,
            state="build_failed",
            terminal_failure_code="scripted_build_failure",
            failure_evidence_hash=_hash("a"),
        )
        self.repository.record_round_candidate_build(built)
        with self.assertRaises(Conflict):
            self.repository.freeze_search_round_artifact_family(
                ArtifactFamilyFreezeRequest(
                    round_id=request.round_id,
                    candidate_family_hash=closed["candidate_family_hash"],
                    expected_artifact_family_hash=_hash("b"),
                )
            )
        self.repository.record_round_candidate_build(failed)

        summary = self.repository.search_round_summary(request.round_id)
        expected = artifact_family_hash(summary["round"], summary["candidates"])
        freeze = ArtifactFamilyFreezeRequest(
            round_id=request.round_id,
            candidate_family_hash=closed["candidate_family_hash"],
            expected_artifact_family_hash=expected,
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            frozen = list(
                pool.map(
                    lambda _: self.repository.freeze_search_round_artifact_family(freeze),
                    range(2),
                )
            )

        self.assertEqual(frozen[0]["artifact_family_hash"], expected)
        self.assertEqual(frozen[1]["artifact_family_hash"], expected)
        self.assertEqual(frozen[0]["state"], "correctness")
        self.assertEqual(frozen[0]["version"], 5)
        replay = self.repository.record_round_candidate_build(built)
        self.assertEqual(replay["artifact_id"], artifact_id)
        with self.assertRaises(Conflict):
            self.repository.record_round_candidate_build(
                built.model_copy(update={"artifact_hash": _hash("c")})
            )
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT event_type, count(*) FROM task_events
                WHERE event_type IN (
                    'm2_round_candidate_build_terminal',
                    'm2_round_artifact_family_frozen'
                ) GROUP BY event_type
                """
            )
            counts = dict(cursor.fetchall())
        self.assertEqual(counts["m2_round_candidate_build_terminal"], 2)
        self.assertEqual(counts["m2_round_artifact_family_frozen"], 1)

    def test_scripted_round_zero_and_holdout_paths_finalize_atomically(self) -> None:
        for promote in (False, True):
            with self.subTest(promote=promote):
                store, holdout_authority, request, search_members, decision = (
                    self._prepare_scripted_evaluation(promote=promote)
                )
                self.repository = PostgresRepository(
                    DATABASE_URL,
                    m2_scripted_finalizer=M2ScriptedRoundFinalizer(store),
                )
                with ThreadPoolExecutor(max_workers=2) as pool:
                    closed = list(
                        pool.map(
                            lambda _, value=decision: (
                                self.repository.close_scripted_search_barrier(value)
                            ),
                            range(2),
                        )
                    )
                self.assertEqual(closed[0]["state"], "search_barrier")
                self.assertEqual(closed[0]["version"], closed[1]["version"])

                holdout_barrier = None
                multiple_comparison = None
                if promote:
                    round_authority = self._round_authority(closed[0])
                    reveal_lease_id = uuid4()
                    execution_lease_id = uuid4()
                    now = datetime.now(timezone.utc)
                    holdout_authority.issue_reveal_lease(
                        round_authority=round_authority,
                        authorized_worker_id="m2-scripted-measurement-worker",
                        execution_lease_id=execution_lease_id,
                        resource_id="scripted-resource",
                        fencing_token=9,
                        issued_at=now,
                        expires_at=now + timedelta(minutes=5),
                        reveal_lease_id=reveal_lease_id,
                    )
                    reveal = holdout_authority.reveal(
                        reveal_lease_id=reveal_lease_id,
                        round_id=request.round_id,
                        authorized_worker_id="m2-scripted-measurement-worker",
                        execution_lease_id=execution_lease_id,
                        resource_id="scripted-resource",
                        fencing_token=9,
                        revealed_at=now,
                    )
                    self.assertEqual(
                        store.publish_bytes(reveal.canonical_plan_json.encode()).sha256,
                        reveal.plan_hash,
                    )
                    self.assertEqual(
                        store.publish(reveal.evidence_payload()).sha256,
                        reveal.reveal_evidence_hash,
                    )
                    revealed = self.repository.record_scripted_holdout_reveal(reveal)
                    self.assertEqual(revealed["state"], "holdout_measuring")
                    holdout_authority_row = self._round_authority(revealed).model_copy(
                        update={"state": SearchRoundState.HOLDOUT_BARRIER}
                    )
                    search_by_id = {item.candidate_id: item for item in search_members}
                    holdout_members: list[BarrierMemberResult] = []
                    holdout_statistics: list[ScriptedCandidateStatisticsInput] = []
                    for ordinal, candidate_id in enumerate(
                        decision.barrier.promoted_candidate_ids
                    ):
                        search_member = search_by_id[candidate_id]
                        member = BarrierMemberResult(
                            round_candidate_id=search_member.round_candidate_id,
                            candidate_id=candidate_id,
                            candidate_state=RoundCandidateState.HOLDOUT_MEASURED,
                            artifact_id=search_member.artifact_id,
                            artifact_hash=search_member.artifact_hash,
                            correctness_evidence_hash=(
                                search_member.correctness_evidence_hash
                            ),
                            scripted_phase_receipt_id=uuid4(),
                            budget_usage_evidence_hash=self._publish(
                                store,
                                f"budget-holdout-{request.round_id}-{ordinal}",
                            ).sha256,
                            cleanup_evidence_hash=self._publish(
                                store,
                                f"cleanup-holdout-{request.round_id}-{ordinal}",
                            ).sha256,
                            synthetic=True,
                        )
                        holdout_members.append(member)
                        holdout_statistics.append(
                            ScriptedCandidateStatisticsInput(
                                candidate_id=candidate_id,
                                scripted_phase_receipt_id=(
                                    member.scripted_phase_receipt_id
                                ),
                                correctness_evidence_hash=(
                                    member.correctness_evidence_hash
                                ),
                                raw_evidence_hash=self._publish(
                                    store,
                                    f"raw-holdout-{request.round_id}-{ordinal}",
                                ).sha256,
                                baseline_sample_set_hash=self._publish(
                                    store,
                                    f"baseline-holdout-{request.round_id}-{ordinal}",
                                ).sha256,
                                restart_effects=(0.20 + ordinal * 0.02,) * 4,
                                baseline_restart_means_ns=(100.0,) * 4,
                                stage0_mde_ratio=0.03,
                            )
                        )
                    holdout_barrier = close_scripted_holdout_barrier(
                        round_authority=holdout_authority_row,
                        expected_candidate_ids=decision.barrier.promoted_candidate_ids,
                        members=tuple(holdout_members),
                        closed_by="m2-scripted-evaluation-authority",
                        closed_at=now,
                        idempotency_key=f"m2-holdout-barrier-{request.round_id}",
                    )
                    held = self.repository.close_scripted_holdout_barrier(
                        holdout_barrier
                    )
                    self.assertEqual(held["state"], "holdout_barrier")
                    multiple_comparison = bonferroni_fwer(
                        round_authority=self._round_authority(held),
                        holdout_barrier=holdout_barrier,
                        statistics=tuple(holdout_statistics),
                        created_at=now,
                    )
                    replayed = self.repository.record_scripted_multiple_comparison(
                        multiple_comparison
                    )
                    self.assertEqual(
                        replayed.result_hash, multiple_comparison.result_hash
                    )

                summary = self.repository.search_round_summary(request.round_id)
                budget_document = round_budget_ledger_document(
                    request.round_id,
                    summary["budget_reservations"],
                    summary["budget_ledger"],
                )
                budget_artifact = store.publish(budget_document)
                authority = self._round_authority(summary["round"])
                requirements = scripted_round_evidence_requirements(
                    round_authority=authority,
                    search_decision=decision,
                    budget_ledger_hash=budget_artifact.sha256,
                    holdout_barrier=holdout_barrier,
                    multiple_comparison=multiple_comparison,
                )
                published = publish_scripted_evidence_index(
                    store=store,
                    round_id=request.round_id,
                    requirements=requirements,
                    producer="m2-scripted-evaluation-authority",
                    retention_owner="m2-postgres-integration",
                    created_at=datetime.now(timezone.utc),
                )
                bundle = build_scripted_round_evidence(
                    round_authority=authority,
                    search_decision=decision,
                    budget_ledger_hash=budget_artifact.sha256,
                    evidence_index_uri=published.artifact.uri,
                    evidence_index_hash=published.artifact.sha256,
                    evidence_reader=store,
                    holdout_barrier=holdout_barrier,
                    multiple_comparison=multiple_comparison,
                )
                with ThreadPoolExecutor(max_workers=2) as pool:
                    terminal = list(
                        pool.map(
                            lambda _, value=bundle: (
                                self.repository.finalize_scripted_search_round(value)
                            ),
                            range(2),
                        )
                    )
                self.assertEqual(terminal[0]["state"], "scripted_completed")
                self.assertEqual(terminal[0]["version"], terminal[1]["version"])
                final_summary = self.repository.search_round_summary(request.round_id)
                self.assertIsNotNone(final_summary["evidence_bundle"])
                self.assertFalse(final_summary["automatic_release_allowed"])
                with self.connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT event_type, count(*) FROM task_events
                        WHERE task_id = %s AND event_type IN (
                            'm2_search_barrier_closed',
                            'm2_holdout_plan_revealed',
                            'm2_holdout_barrier_closed',
                            'm2_multiple_comparison_recorded',
                            'm2_scripted_round_completed'
                        ) GROUP BY event_type
                        """,
                        (request.task_id,),
                    )
                    event_counts = dict(cursor.fetchall())
                self.assertEqual(event_counts["m2_search_barrier_closed"], 1)
                self.assertEqual(event_counts["m2_scripted_round_completed"], 1)
                if promote:
                    self.assertEqual(event_counts["m2_holdout_plan_revealed"], 1)
                    self.assertEqual(event_counts["m2_holdout_barrier_closed"], 1)
                    self.assertEqual(
                        event_counts["m2_multiple_comparison_recorded"], 1
                    )
