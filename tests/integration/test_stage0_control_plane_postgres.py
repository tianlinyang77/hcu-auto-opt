import os
import threading
import unittest
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hcuopt.contracts.platform_v1 import AdapterProvenance, TargetSpec
from hcuopt.contracts.v1 import Stage0ProbeResult, Stage0RunCreate, WorkerRegister
from hcuopt.domain.enums import (
    ProjectMode,
    Stage0ProbeType,
    Stage0RunMode,
    Stage0RunState,
    TaskState,
    WorkerType,
)
from hcuopt.domain.errors import Conflict
from hcuopt.orchestrator.stage0 import Stage0Coordinator
from hcuopt.storage.repository import PostgresRepository
from hcuopt.targets import load_target

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

DATABASE_URL = os.getenv("HCUOPT_DATABASE_URL")
ROOT = Path(__file__).parents[2]
TARGET_PATH = ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml"
PROFILE = "real-stage0-fixture"


@unittest.skipUnless(DATABASE_URL and psycopg, "requires PostgreSQL and psycopg")
@pytest.mark.postgres
class Stage0ControlPlanePostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        assert DATABASE_URL is not None
        assert psycopg is not None
        self.repository = PostgresRepository(DATABASE_URL)
        self.repository.migrate()
        self.connection = psycopg.connect(DATABASE_URL)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                TRUNCATE framework_smoke_signoffs, stage0_probe_records, stage0_runs,
                    task_events, evidence_bundles, execution_requests, source_snapshots,
                    job_events, execution_attempts, evaluation_runs, artifacts,
                    candidates, hotspots, jobs, workers, stage0_evidence,
                    baseline_epochs, resources, tasks, target_snapshots
                RESTART IDENTITY CASCADE
                """
            )
        self.connection.commit()
        target = load_target(TARGET_PATH)
        self.target = target.model_copy(
            update={
                "blockers": [
                    blocker.model_copy(update={"status": "accepted"})
                    if blocker.status == "open" and "stage0" in blocker.blocks
                    else blocker
                    for blocker in target.blockers
                ]
            }
        )
        self.coordinator = Stage0Coordinator(self.repository)

    def tearDown(self) -> None:
        self.connection.close()

    @staticmethod
    def _summary(probe_type: Stage0ProbeType) -> dict[str, object]:
        summaries: dict[Stage0ProbeType, dict[str, object]] = {
            Stage0ProbeType.FINGERPRINT: {
                "hardware_fingerprint": "sha256:hardware",
                "software_fingerprint": "sha256:software",
            },
            Stage0ProbeType.TIMER: {"timer_resolution_ns": 100.0},
            Stage0ProbeType.NOISE: {
                "gate_result": "pass",
                "noise_sigma_ns": 200.0,
                "noise_cv": 0.01,
                "mde_ratio": 0.03,
            },
            Stage0ProbeType.KNOWN_SIGNAL: {"detected": True},
            Stage0ProbeType.NULL_SIGNAL: {"false_positive": False},
            Stage0ProbeType.PROFILER: {"capability": "full"},
            Stage0ProbeType.HOTPATCH: {"capability": "hot_patch"},
        }
        return summaries[probe_type]

    def _create(self, key: str, mode: Stage0RunMode) -> dict:
        return self.repository.create_stage0_run(
            Stage0RunCreate(
                name=f"Stage 0 {key}",
                workload_id="sglang-qwen2.5-0.5b",
                target_id=self.target.target_id,
                adapter_profile=PROFILE,
                mode=mode,
                protocol_version="stage0-fixture-v1",
                idempotency_key=key,
            ),
            self.target,
            str(TARGET_PATH),
        )

    def test_formal_probe_jobs_open_barrier_and_finalize_idempotently(self) -> None:
        run = self._create("formal-stage0-fixture", Stage0RunMode.FORMAL)
        self.repository.register_worker(
            WorkerRegister(
                worker_id="stage0-gpu",
                worker_type=WorkerType.GPU,
                adapter_profile=PROFILE,
                capabilities={"resource_id": "fixture-hcu-7"},
            )
        )
        provenance = AdapterProvenance(
            profile=PROFILE,
            capability="stage0_probe",
            adapter_name="RealStage0Fixture",
            adapter_version="1",
            implementation_kind="real",
        )

        for _ in range(len(Stage0ProbeType)):
            job = self.repository.claim_job("stage0-gpu")
            assert job is not None
            probe_type = Stage0ProbeType(job["payload"]["probe_type"])
            result = Stage0ProbeResult(
                stage0_run_id=run["stage0_run_id"],
                target_snapshot_id=run["target_snapshot_id"],
                probe_type=probe_type,
                protocol_version=run["protocol_version"],
                raw_evidence_uri=f"file:///stage0/{probe_type.value}.json",
                raw_evidence_hash="sha256:" + "0" * 64,
                summary=self._summary(probe_type),
                adapter_provenance=[provenance],
                synthetic=False,
                cleanup_evidence={
                    "fence": {"fenced": True},
                    "health": {"healthy": True},
                },
            )
            completed = self.repository.complete_job(
                job["job_id"],
                job["claim_token"],
                job["fencing_token"],
                result.model_dump(mode="json"),
            )
            self.coordinator.advance(completed)

        ready = self.repository.get_stage0_run(run["stage0_run_id"])
        self.assertEqual(ready["state"], Stage0RunState.READY.value)
        self.assertEqual(
            len(self.repository.list_stage0_probe_records(run["stage0_run_id"])),
            len(Stage0ProbeType),
        )

        barrier = threading.Barrier(2)
        reports: list[dict] = []
        errors: list[BaseException] = []

        def finalize() -> None:
            try:
                barrier.wait()
                reports.append(
                    self.repository.finalize_stage0_run(run["stage0_run_id"])
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=finalize) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(reports), 2)
        self.assertEqual(reports[0], reports[1])
        report = reports[0]
        replay = self.repository.finalize_stage0_run(run["stage0_run_id"])
        self.assertEqual(report, replay)
        self.assertEqual(report["mode"], ProjectMode.FULL_MVP.value)
        self.assertFalse(report["automatic_release_allowed"])
        task = self.repository.get_task(run["task_id"])
        self.assertEqual(task["state"], TaskState.BASELINE_PENDING.value)

    def test_stage0_creation_is_concurrently_idempotent(self) -> None:
        barrier = threading.Barrier(2)
        run_ids: list[str] = []
        errors: list[BaseException] = []

        def create() -> None:
            try:
                barrier.wait()
                run = self._create("concurrent-stage0-fixture", Stage0RunMode.DRY_RUN)
                run_ids.append(str(run["stage0_run_id"]))
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=create) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(set(run_ids)), 1)
        summary = self.repository.stage0_run_summary(UUID(run_ids[0]))
        self.assertEqual(len(summary["jobs"]), len(Stage0ProbeType))

    def test_target_snapshot_upsert_is_concurrently_idempotent(self) -> None:
        thread_count = 4

        def upsert(
            barrier: threading.Barrier,
            target: TargetSpec,
            snapshot_ids: list[str],
            errors: list[BaseException],
        ) -> None:
            try:
                barrier.wait()
                snapshot = self.repository.upsert_target_snapshot(
                    target, str(TARGET_PATH)
                )
                snapshot_ids.append(str(snapshot["target_snapshot_id"]))
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        for attempt in range(20):
            target = self.target.model_copy(
                update={"target_id": f"{self.target.target_id}-race-{attempt}"}
            )
            barrier = threading.Barrier(thread_count)
            snapshot_ids: list[str] = []
            errors: list[BaseException] = []

            threads = [
                threading.Thread(
                    target=upsert, args=(barrier, target, snapshot_ids, errors)
                )
                for _ in range(thread_count)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [], f"attempt {attempt}: {errors!r}")
            self.assertEqual(len(snapshot_ids), thread_count)
            self.assertEqual(len(set(snapshot_ids)), 1)

    def test_probe_record_is_concurrently_idempotent(self) -> None:
        run = self._create("concurrent-probe-fixture", Stage0RunMode.DRY_RUN)
        self.repository.register_worker(
            WorkerRegister(
                worker_id="stage0-dry-run-gpu",
                worker_type=WorkerType.GPU,
                adapter_profile=PROFILE,
            )
        )
        job = self.repository.claim_job("stage0-dry-run-gpu")
        assert job is not None
        probe_type = Stage0ProbeType(job["payload"]["probe_type"])
        result = Stage0ProbeResult(
            stage0_run_id=run["stage0_run_id"],
            target_snapshot_id=run["target_snapshot_id"],
            probe_type=probe_type,
            protocol_version=run["protocol_version"],
            summary=self._summary(probe_type),
            adapter_provenance=[
                AdapterProvenance(
                    profile=PROFILE,
                    capability="stage0_probe",
                    adapter_name="SyntheticStage0Fixture",
                    adapter_version="1",
                    implementation_kind="fake",
                )
            ],
            synthetic=True,
        )
        completed = self.repository.complete_job(
            job["job_id"],
            job["claim_token"],
            job["fencing_token"],
            result.model_dump(mode="json"),
        )
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def record() -> None:
            try:
                barrier.wait()
                self.coordinator.advance(completed)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=record) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(
            len(self.repository.list_stage0_probe_records(run["stage0_run_id"])), 1
        )
        events = self.repository.list_task_events(run["task_id"])
        self.assertEqual(
            len(
                [
                    event
                    for event in events
                    if event["event_type"] == "stage0_probe_recorded"
                ]
            ),
            1,
        )

    def test_probe_result_cannot_cross_target_snapshots(self) -> None:
        run = self._create("cross-target-probe-fixture", Stage0RunMode.DRY_RUN)
        self.repository.register_worker(
            WorkerRegister(
                worker_id="stage0-cross-target-gpu",
                worker_type=WorkerType.GPU,
                adapter_profile=PROFILE,
            )
        )
        job = self.repository.claim_job("stage0-cross-target-gpu")
        assert job is not None
        probe_type = Stage0ProbeType(job["payload"]["probe_type"])
        result = Stage0ProbeResult(
            stage0_run_id=run["stage0_run_id"],
            target_snapshot_id=uuid4(),
            probe_type=probe_type,
            protocol_version=run["protocol_version"],
            summary=self._summary(probe_type),
            adapter_provenance=[
                AdapterProvenance(
                    profile=PROFILE,
                    capability="stage0_probe",
                    adapter_name="SyntheticStage0Fixture",
                    adapter_version="1",
                    implementation_kind="fake",
                )
            ],
            synthetic=True,
        )
        completed = self.repository.complete_job(
            job["job_id"],
            job["claim_token"],
            job["fencing_token"],
            result.model_dump(mode="json"),
        )

        with self.assertRaisesRegex(Conflict, "different snapshot"):
            self.coordinator.advance(completed)
        self.assertEqual(
            self.repository.list_stage0_probe_records(run["stage0_run_id"]), []
        )

    def test_dry_run_cannot_be_finalized_as_formal_evidence(self) -> None:
        run = self._create("dry-stage0-fixture", Stage0RunMode.DRY_RUN)
        with self.assertRaisesRegex(Conflict, "Dry Run evidence"):
            self.repository.finalize_stage0_run(run["stage0_run_id"])
