import os
import threading
import unittest
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    ArtifactManifest,
    EvaluationRun,
    EvidenceBundle,
    MeasurementSeries,
    SourceSnapshot,
)
from hcuopt.contracts.v1 import (
    FrameworkSmokeCreate,
    ManualCandidateAdjudicationResult,
    ManualCandidateBuildResult,
    ManualCandidateCreate,
    ManualCandidateSignoffRequest,
    ManualCandidateTaskCreate,
    ManualCorrectnessResult,
    ManualPerformanceEvidenceResult,
    WorkerRegister,
)
from hcuopt.domain.enums import (
    CandidateState,
    JobType,
    ManualCandidateDecision,
    ManualCandidateVerdict,
    TaskState,
    WorkerType,
)
from hcuopt.domain.errors import Conflict, StaleClaimToken
from hcuopt.orchestrator.manual_candidate import ManualCandidateCoordinator
from hcuopt.storage.repository import PostgresRepository
from hcuopt.targets import load_target

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover
    psycopg = None
    Jsonb = None

DATABASE_URL = os.getenv("HCUOPT_DATABASE_URL")
ROOT = Path(__file__).parents[2]
TARGET_PATH = ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml"
PROFILE = "m1-real-test"


@unittest.skipUnless(DATABASE_URL and psycopg, "requires PostgreSQL and psycopg")
@pytest.mark.postgres
class M1ControlPlanePostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        assert DATABASE_URL is not None
        assert psycopg is not None
        assert Jsonb is not None
        self.repository = PostgresRepository(DATABASE_URL)
        self.repository.migrate()
        self.connection = psycopg.connect(DATABASE_URL)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                TRUNCATE manual_candidate_signoffs, framework_smoke_signoffs,
                    stage0_probe_records, stage0_runs, task_events,
                    evidence_bundles, execution_requests, source_snapshots,
                    job_events, execution_attempts, evaluation_runs, artifacts,
                    candidates, hotspots, jobs, workers, stage0_evidence,
                    baseline_epochs, resources, tasks, target_snapshots
                RESTART IDENTITY CASCADE
                """
            )
        self.connection.commit()
        self.target = load_target(TARGET_PATH)
        self.target_snapshot = self.repository.upsert_target_snapshot(
            self.target, str(TARGET_PATH)
        )
        self.provenance = AdapterProvenance(
            profile=PROFILE,
            capability="fixture_real_source",
            adapter_name="M1PostgresFixture",
            adapter_version="1",
            implementation_kind="real",
        )
        self.baseline_source = self._seed_baseline_source()
        self.stage0_run_id = self._seed_formal_stage0()

    def tearDown(self) -> None:
        self.connection.close()

    def _seed_baseline_source(self) -> SourceSnapshot:
        task = self.repository.create_framework_smoke_task(
            FrameworkSmokeCreate(
                name="M1 baseline source fixture",
                target_id=self.target.target_id,
                adapter_profile=PROFILE,
                idempotency_key="m1-baseline-source-fixture",
            ),
            self.target,
            str(TARGET_PATH),
        )
        source = SourceSnapshot(
            kind="baseline",
            repository=self.target.source_baseline.repository,
            commit=self.target.source_baseline.commit,
            tree_hash="a" * 40,
            source_hash="sha256:" + "b" * 64,
            worktree_uri="file:///m1/baseline",
            clean=True,
        )
        self.repository.record_source_snapshot(
            task["task_id"],
            source,
            [self.provenance.model_dump(mode="json")],
            False,
            "m1-baseline-source-snapshot",
        )
        # The Framework task is only a durable owner for this trusted SourceSnapshot;
        # its real source-preparation Job is outside this M1 repository fixture.
        with self.connection.cursor() as cursor:
            cursor.execute("DELETE FROM jobs WHERE task_id = %s", (task["task_id"],))
        self.connection.commit()
        return source

    def _seed_formal_stage0(self):
        task_id = uuid4()
        run_id = uuid4()
        evidence = {
            "source": "independently-verified-raw-evidence",
            "stage0_run_id": str(run_id),
            "target_snapshot_id": str(self.target_snapshot["target_snapshot_id"]),
            "protocol_version": "s0-g0-v2",
            "protocol_hash": "sha256:" + "c" * 64,
            "input_digest": "sha256:" + "d" * 64,
            "measurement": "pass",
            "profiler": "degraded",
            "hot_patch": "overlay_only",
            "timer_resolution_ns": 100.0,
            "noise_sigma_ns": 200.0,
            "noise_cv": 0.01,
            "mde_ratio": 0.03,
            "synthetic": False,
        }
        report = {
            "task_id": str(task_id),
            "mode": "degraded_manual_intake",
            "automatic_release_allowed": False,
            "evidence_authority": "formal",
            "protocol_version": "s0-g0-v2",
            "protocol_hash": "sha256:" + "c" * 64,
            "input_digest": "sha256:" + "d" * 64,
            "machine_report_uri": "file:///m1/stage0-report.json",
            "machine_report_hash": "sha256:" + "a" * 64,
        }
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO tasks (
                    task_id, name, workload_id, idempotency_key, state,
                    project_mode, automatic_release_allowed, workflow_type,
                    target_id, target_snapshot_id, adapter_profile,
                    stage0_authority
                ) VALUES (
                    %s, 'Formal S0 fixture', 'nmz36-sglang-smoke-v1',
                    'm1-formal-stage0-fixture', 'degraded',
                    'degraded_manual_intake', FALSE, 'stage0', %s, %s,
                    'nmz36-stage0-v2', 'formal'
                )
                """,
                (
                    task_id,
                    self.target.target_id,
                    self.target_snapshot["target_snapshot_id"],
                ),
            )
            cursor.execute(
                """
                INSERT INTO stage0_runs (
                    stage0_run_id, task_id, target_snapshot_id,
                    adapter_profile, mode, state, protocol_version,
                    idempotency_key, report, finalized_at
                ) VALUES (
                    %s, %s, %s, 'nmz36-stage0-v2', 'formal', 'finalized',
                    's0-g0-v2', 'm1-formal-stage0-fixture', %s, now()
                )
                """,
                (
                    run_id,
                    task_id,
                    self.target_snapshot["target_snapshot_id"],
                    Jsonb(report),
                ),
            )
            cursor.execute(
                """
                INSERT INTO stage0_evidence (
                    task_id, stage0_run_id, evidence, report
                ) VALUES (%s, %s, %s, %s)
                """,
                (task_id, run_id, Jsonb(evidence), Jsonb(report)),
            )
        self.connection.commit()
        return run_id

    def _task_request(self, key: str = "m1-task-fixture") -> ManualCandidateTaskCreate:
        return ManualCandidateTaskCreate(
            name="M1 manual Candidate fixture",
            stage0_run_id=self.stage0_run_id,
            adapter_profile=PROFILE,
            baseline_source_snapshot_id=self.baseline_source.snapshot_id,
            workload_hash="sha256:" + "e" * 64,
            configuration_hash="sha256:" + "f" * 64,
            idempotency_key=key,
            budget={"max_wall_seconds": 600, "max_samples": 1000},
        )

    def _candidate_request(
        self,
        task_id: UUID,
        key: str = "m1-candidate-fixture",
    ) -> ManualCandidateCreate:
        baseline = self.repository.get_baseline(task_id)
        assert baseline is not None
        return ManualCandidateCreate(
            baseline_epoch_id=baseline["baseline_epoch_id"],
            source_hash="sha256:" + "1" * 64,
            optimization_intent="replace one manually located LayerNorm hotspot",
            replacement_point="sglang.srt.layers.layernorm",
            candidate_kind="business",
            idempotency_key=key,
        )

    def test_task_and_candidate_are_concurrently_idempotent_and_bound(self) -> None:
        request = self._task_request()
        barrier = threading.Barrier(2)
        task_ids: list[str] = []
        errors: list[BaseException] = []

        def create_task() -> None:
            try:
                barrier.wait()
                row = PostgresRepository(DATABASE_URL).create_manual_candidate_task(
                    request
                )
                task_ids.append(str(row["task_id"]))
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=create_task) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(task_ids)), 1)
        task_id = UUID(next(iter(task_ids)))
        task = self.repository.get_task(task_id)
        self.assertEqual(task["workflow_type"], "manual_candidate")
        self.assertEqual(task["stage0_run_id"], self.stage0_run_id)
        self.assertFalse(task["automatic_release_allowed"])
        baseline = self.repository.get_baseline(task["task_id"])
        assert baseline is not None
        self.assertEqual(
            baseline["source_snapshot_id"], self.baseline_source.snapshot_id
        )
        self.assertEqual(
            baseline["target_snapshot_id"],
            self.target_snapshot["target_snapshot_id"],
        )
        self.assertEqual(baseline["stage0_run_id"], self.stage0_run_id)
        self.assertEqual(baseline["stage0_protocol_hash"], "sha256:" + "c" * 64)

        with self.assertRaises(Conflict):
            self.repository.create_manual_candidate(
                task["task_id"],
                self._candidate_request(task["task_id"]).model_copy(
                    update={"baseline_epoch_id": uuid4()}
                ),
            )

        candidate_barrier = threading.Barrier(2)
        candidate_ids: list[str] = []
        candidate_errors: list[BaseException] = []

        def create_candidate() -> None:
            try:
                candidate_barrier.wait()
                row = PostgresRepository(DATABASE_URL).create_manual_candidate(
                    task["task_id"], self._candidate_request(task["task_id"])
                )
                candidate_ids.append(str(row["candidate_id"]))
            except BaseException as exc:  # pragma: no cover - asserted below
                candidate_errors.append(exc)

        threads = [threading.Thread(target=create_candidate) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(candidate_errors, [])
        self.assertEqual(len(set(candidate_ids)), 1)
        summary = self.repository.manual_candidate_summary(task["task_id"])
        self.assertEqual(summary["candidate"]["state"], CandidateState.BUILDING.value)
        self.assertEqual(len(summary["jobs"]), 1)
        self.assertEqual(summary["jobs"][0]["job_type"], JobType.MANUAL_BUILD.value)
        self.assertEqual(summary["jobs"][0]["lease_scope"], "none")
        self.assertEqual(
            summary["jobs"][0]["payload"]["stage0_report"],
            {
                "uri": "file:///m1/stage0-report.json",
                "sha256": "sha256:" + "a" * 64,
                "input_digest": "sha256:" + "d" * 64,
                "protocol_version": "s0-g0-v2",
                "protocol_hash": "sha256:" + "c" * 64,
            },
        )

        with self.assertRaises(Conflict):
            self.repository.create_manual_candidate(
                task["task_id"],
                self._candidate_request(task["task_id"], "different-candidate-key"),
            )

        other_task = self.repository.create_manual_candidate_task(
            self._task_request("m1-other-task-same-candidate-key")
        )
        with self.assertRaises(Conflict):
            self.repository.create_manual_candidate(
                other_task["task_id"],
                self._candidate_request(other_task["task_id"]),
            )

    def test_baseline_is_immutable_and_terminal_build_failure_converges(self) -> None:
        task = self.repository.create_manual_candidate_task(self._task_request())
        candidate = self.repository.create_manual_candidate(
            task["task_id"], self._candidate_request(task["task_id"])
        )
        with self.assertRaises(psycopg.Error):
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE baseline_epochs SET workload_hash = %s
                    WHERE task_id = %s
                    """,
                    ("sha256:" + "9" * 64, task["task_id"]),
                )
            self.connection.commit()
        self.connection.rollback()

        self.repository.register_worker(
            WorkerRegister(
                worker_id="m1-build-worker",
                worker_type=WorkerType.BUILD,
                adapter_profile=PROFILE,
            )
        )
        job = self.repository.claim_job("m1-build-worker")
        assert job is not None
        failed = self.repository.fail_job(
            job["job_id"],
            job["claim_token"],
            job["fencing_token"],
            {"code": "fixture_failure", "message": "expected terminal failure"},
            retryable=False,
        )
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(
            self.repository.get_task(task["task_id"])["state"],
            TaskState.REJECTED.value,
        )
        self.assertEqual(
            self.repository.get_candidate(candidate["candidate_id"])["state"],
            CandidateState.BUILD_FAILED.value,
        )
        with self.assertRaises(StaleClaimToken):
            self.repository.fail_job(
                job["job_id"],
                job["claim_token"],
                job["fencing_token"],
                {"code": "replay", "message": "stale replay"},
                retryable=False,
            )

    def test_duplicate_job_completion_is_one_durable_result(self) -> None:
        task = self.repository.create_manual_candidate_task(
            self._task_request("m1-complete-replay-task")
        )
        candidate = self.repository.create_manual_candidate(
            task["task_id"],
            self._candidate_request(task["task_id"], "m1-complete-replay-candidate"),
        )
        self.repository.register_worker(
            WorkerRegister(
                worker_id="m1-complete-replay-worker",
                worker_type=WorkerType.BUILD,
                adapter_profile=PROFILE,
            )
        )
        job = self.repository.claim_job("m1-complete-replay-worker")
        assert job is not None
        result = {
            "candidate_id": str(candidate["candidate_id"]),
            "fixture": "repository-completion-replay",
        }
        first = self.repository.complete_job(
            job["job_id"], job["claim_token"], job["fencing_token"], result
        )
        replay = self.repository.complete_job(
            job["job_id"], job["claim_token"], job["fencing_token"], result
        )
        self.assertEqual(first["result"], replay["result"])
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*) FROM job_events
                WHERE job_id = %s AND event_type = 'succeeded'
                """,
                (job["job_id"],),
            )
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_terminal_stale_worker_converges_m1_task_and_candidate(self) -> None:
        task = self.repository.create_manual_candidate_task(
            self._task_request("m1-stale-worker-task")
        )
        candidate = self.repository.create_manual_candidate(
            task["task_id"],
            self._candidate_request(task["task_id"], "m1-stale-worker-candidate"),
        )
        self.repository.register_worker(
            WorkerRegister(
                worker_id="m1-stale-build-worker",
                worker_type=WorkerType.BUILD,
                adapter_profile=PROFILE,
            )
        )
        job = self.repository.claim_job("m1-stale-build-worker")
        assert job is not None
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE jobs
                SET max_attempts = attempts,
                    heartbeat_at = now() - interval '10 minutes'
                WHERE job_id = %s
                """,
                (job["job_id"],),
            )
        self.connection.commit()

        recovered = self.repository.recover_stale_jobs(stale_after_seconds=0)

        self.assertEqual(recovered, [job["job_id"]])
        self.assertEqual(
            self.repository.get_task(task["task_id"])["state"],
            TaskState.REJECTED.value,
        )
        self.assertEqual(
            self.repository.get_candidate(candidate["candidate_id"])["state"],
            CandidateState.BUILD_FAILED.value,
        )
        summary = self.repository.manual_candidate_summary(task["task_id"])
        self.assertEqual(summary["jobs"][0]["state"], "failed")
        self.assertTrue(
            any(
                event["event_type"] == "manual_candidate_job_failed"
                for event in summary["events"]
            )
        )

    def test_full_manual_candidate_workflow_preserves_evidence_boundaries(self) -> None:
        task = self.repository.create_manual_candidate_task(
            self._task_request("m1-full-workflow-task")
        )
        candidate = self.repository.create_manual_candidate(
            task["task_id"],
            self._candidate_request(task["task_id"], "m1-full-workflow-candidate"),
        )
        coordinator = ManualCandidateCoordinator(self.repository)
        self.repository.register_worker(
            WorkerRegister(
                worker_id="m1-full-build-worker",
                worker_type=WorkerType.BUILD,
                adapter_profile=PROFILE,
            )
        )
        build_job = self.repository.claim_job("m1-full-build-worker")
        assert build_job is not None
        candidate_source = SourceSnapshot(
            kind="candidate",
            repository=self.baseline_source.repository,
            commit=self.baseline_source.commit,
            tree_hash="3" * 40,
            source_hash=candidate["source_hash"],
            worktree_uri="file:///m1/candidate",
            clean=True,
            parent_snapshot_id=self.baseline_source.snapshot_id,
        )
        artifact = ArtifactManifest(
            candidate_id=candidate["candidate_id"],
            kind="python_overlay",
            uri="file:///m1/overlay.py",
            content_hash="sha256:" + "4" * 64,
            source_snapshot_id=candidate_source.snapshot_id,
        )
        build_result = ManualCandidateBuildResult(
            candidate_id=candidate["candidate_id"],
            source=candidate_source,
            artifact=artifact,
            adapter_provenance=[self.provenance],
        ).model_dump(mode="json")
        completed_build = self.repository.complete_job(
            build_job["job_id"],
            build_job["claim_token"],
            build_job["fencing_token"],
            build_result,
        )
        coordinator.advance(completed_build)
        replayed_build = self.repository.complete_job(
            build_job["job_id"],
            build_job["claim_token"],
            build_job["fencing_token"],
            build_result,
        )
        coordinator.advance(replayed_build)

        self.repository.register_worker(
            WorkerRegister(
                worker_id="m1-full-gpu-worker",
                worker_type=WorkerType.GPU,
                adapter_profile=PROFILE,
                capabilities={"resource_id": "m1-fixture-hcu"},
            )
        )
        correctness_job = self.repository.claim_job("m1-full-gpu-worker")
        assert correctness_job is not None
        self.assertEqual(correctness_job["job_type"], JobType.MANUAL_CORRECTNESS.value)
        correctness_uri = "file:///m1/correctness.json"
        correctness_result = ManualCorrectnessResult(
            candidate_id=candidate["candidate_id"],
            verdict="correct",
            protocol_version="m1-correctness-v1",
            raw_evidence_uri=correctness_uri,
            raw_evidence_hash="sha256:" + "5" * 64,
            adapter_provenance=[self.provenance],
            cleanup_evidence={
                "fence": {"fenced": True},
                "health": {"healthy": True},
            },
        ).model_dump(mode="json")
        completed_correctness = self.repository.complete_job(
            correctness_job["job_id"],
            correctness_job["claim_token"],
            correctness_job["fencing_token"],
            correctness_result,
        )
        coordinator.advance(completed_correctness)

        performance_job = self.repository.claim_job("m1-full-gpu-worker")
        assert performance_job is not None
        self.assertEqual(performance_job["job_type"], JobType.MANUAL_PERFORMANCE.value)
        measurement = MeasurementSeries(
            status="measured",
            metric_name="latency",
            unit="us",
            protocol_version="m1-performance-v1",
            sample_count=40,
            warmup_count=10,
            process_restart_count=4,
            raw_samples_uri="file:///m1/performance-samples.json",
            raw_samples_hash="sha256:" + "6" * 64,
            environment_fingerprint=performance_job["payload"]["target_fingerprint"],
            summary={"baseline_median_us": 105.0, "candidate_median_us": 100.0},
            adapter_provenance=self.provenance,
        )
        performance_result = ManualPerformanceEvidenceResult(
            candidate_id=candidate["candidate_id"],
            measurement=measurement,
            cleanup_evidence={
                "fence": {"fenced": True},
                "health": {"healthy": True},
            },
        ).model_dump(mode="json")
        completed_performance = self.repository.complete_job(
            performance_job["job_id"],
            performance_job["claim_token"],
            performance_job["fencing_token"],
            performance_result,
        )
        coordinator.advance(completed_performance)

        self.repository.register_worker(
            WorkerRegister(
                worker_id="m1-full-evaluation-worker",
                worker_type=WorkerType.EVALUATION,
                adapter_profile=PROFILE,
            )
        )
        adjudication_job = self.repository.claim_job("m1-full-evaluation-worker")
        assert adjudication_job is not None
        self.assertEqual(adjudication_job["job_type"], JobType.MANUAL_ADJUDICATE.value)
        evaluation = EvaluationRun(
            task_id=task["task_id"],
            candidate_id=candidate["candidate_id"],
            round_id=candidate["round_id"],
            baseline_epoch_id=candidate["baseline_epoch_id"],
            phase="performance",
            protocol_version="m1-adjudication-v1",
            target_fingerprint=adjudication_job["payload"]["target_fingerprint"],
            idempotency_key="m1-full-workflow-evaluation",
            passed=True,
            metrics={"verdict": "faster"},
            measurement=measurement,
            evidence_uris=[correctness_uri, measurement.raw_samples_uri],
            adapter_provenance=[self.provenance],
        )
        evidence = EvidenceBundle(
            task_id=task["task_id"],
            candidate_id=candidate["candidate_id"],
            baseline_epoch_id=candidate["baseline_epoch_id"],
            target_id=task["target_id"],
            evidence_type="m1_manual_candidate",
            protocol_version="m1-adjudication-v1",
            artifact_ids=[artifact.artifact_id],
            measurement_ids=[measurement.measurement_id],
            summary={"verdict": "faster", "automatic_release_allowed": False},
            raw_uris=[correctness_uri, measurement.raw_samples_uri],
            adapter_provenance=[self.provenance],
        )
        adjudication_result = ManualCandidateAdjudicationResult(
            candidate_id=candidate["candidate_id"],
            verdict="faster",
            evaluation=evaluation,
            evidence=evidence,
        ).model_dump(mode="json")
        completed_adjudication = self.repository.complete_job(
            adjudication_job["job_id"],
            adjudication_job["claim_token"],
            adjudication_job["fencing_token"],
            adjudication_result,
        )
        coordinator.advance(completed_adjudication)

        summary = self.repository.manual_candidate_summary(task["task_id"])
        self.assertEqual(summary["task"]["state"], TaskState.AWAITING_SIGNOFF.value)
        self.assertEqual(
            summary["candidate"]["state"], CandidateState.AWAITING_SIGNOFF.value
        )
        self.assertEqual(summary["candidate"]["verdict"], "faster")
        self.assertEqual(
            [job["job_type"] for job in summary["jobs"]],
            [
                JobType.MANUAL_BUILD.value,
                JobType.MANUAL_CORRECTNESS.value,
                JobType.MANUAL_PERFORMANCE.value,
                JobType.MANUAL_ADJUDICATE.value,
            ],
        )
        self.assertEqual(
            [job["lease_scope"] for job in summary["jobs"]],
            ["none", "shared", "exclusive", "none"],
        )

    def test_adjudicated_candidate_signoff_is_idempotent_but_never_releases(self) -> None:
        task = self.repository.create_manual_candidate_task(self._task_request())
        candidate = self.repository.create_manual_candidate(
            task["task_id"], self._candidate_request(task["task_id"])
        )
        for state in (
            CandidateState.BUILT,
            CandidateState.CORRECTNESS_RUNNING,
            CandidateState.PERFORMANCE_RUNNING,
            CandidateState.ADJUDICATING,
            CandidateState.AWAITING_SIGNOFF,
        ):
            self.repository.transition_candidate(candidate["candidate_id"], state)
        for state in (
            TaskState.MANUAL_CORRECTNESS,
            TaskState.MANUAL_PERFORMANCE,
            TaskState.MANUAL_ADJUDICATING,
            TaskState.AWAITING_SIGNOFF,
        ):
            self.repository.transition_task(task["task_id"], state)

        candidate = self.repository.get_candidate(candidate["candidate_id"])
        target = self.repository.get_target_snapshot(task["task_id"])
        measurement = MeasurementSeries(
            status="measured",
            metric_name="latency",
            unit="us",
            protocol_version="m1-single-candidate-v1",
            sample_count=20,
            raw_samples_uri="file:///m1/signoff-samples.json",
            raw_samples_hash="sha256:" + "2" * 64,
            environment_fingerprint=target["target_fingerprint"],
            adapter_provenance=self.provenance,
        )
        evaluation = EvaluationRun(
            task_id=task["task_id"],
            candidate_id=candidate["candidate_id"],
            round_id=candidate["round_id"],
            baseline_epoch_id=candidate["baseline_epoch_id"],
            phase="performance",
            protocol_version="m1-single-candidate-v1",
            target_fingerprint=target["target_fingerprint"],
            idempotency_key="m1-signoff-evaluation",
            passed=None,
            metrics={"verdict": "inconclusive"},
            measurement=measurement,
            adapter_provenance=[self.provenance],
            synthetic=False,
        )
        self.repository.record_evaluation(evaluation)
        evidence = EvidenceBundle(
            task_id=task["task_id"],
            candidate_id=candidate["candidate_id"],
            baseline_epoch_id=candidate["baseline_epoch_id"],
            target_id=task["target_id"],
            evidence_type="m1_manual_candidate",
            protocol_version="m1-single-candidate-v1",
            measurement_ids=[measurement.measurement_id],
            summary={"verdict": "inconclusive", "automatic_release_allowed": False},
            raw_uris=[measurement.raw_samples_uri],
            adapter_provenance=[self.provenance],
            synthetic=False,
        )
        self.repository.record_evidence_bundle(
            evidence, evaluation.evaluation_run_id, "m1-signoff-evidence"
        )
        self.repository.set_manual_candidate_verdict(
            candidate["candidate_id"],
            ManualCandidateVerdict.INCONCLUSIVE,
            evidence.evidence_id,
        )
        request = ManualCandidateSignoffRequest(
            decision=ManualCandidateDecision.APPROVED,
            actor="m1-reviewer",
            reason="accept the evidence, not an automatic release",
            evidence_bundle_id=evidence.evidence_id,
            idempotency_key="m1-signoff-approved",
        )
        barrier = threading.Barrier(2)
        signoffs: list[dict] = []
        errors: list[BaseException] = []

        def signoff() -> None:
            try:
                barrier.wait()
                row = PostgresRepository(DATABASE_URL).signoff_manual_candidate_task(
                    task["task_id"], request
                )
                signoffs.append(row)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=signoff) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({row["signoff_id"] for row in signoffs}), 1)
        first = signoffs[0]
        replay = self.repository.signoff_manual_candidate_task(task["task_id"], request)
        self.assertEqual(first["signoff_id"], replay["signoff_id"])
        final_task = self.repository.get_task(task["task_id"])
        self.assertEqual(final_task["state"], TaskState.COMPLETED.value)
        self.assertFalse(final_task["automatic_release_allowed"])
        self.assertEqual(
            self.repository.get_candidate(candidate["candidate_id"])["state"],
            CandidateState.ACCEPTED.value,
        )
