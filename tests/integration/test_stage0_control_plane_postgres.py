import os
import threading
import unittest
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hcuopt.contracts.platform_v1 import AdapterProvenance, TargetSpec
from hcuopt.contracts.v1 import Stage0ProbeResult, Stage0RunCreate, WorkerRegister
from hcuopt.domain.enums import (
    GateResult,
    HotPatchCapability,
    LeaseScope,
    ProfilerCapability,
    ProjectMode,
    Stage0ProbeType,
    Stage0RunMode,
    Stage0RunState,
    TaskState,
    WorkerType,
)
from hcuopt.domain.errors import Conflict
from hcuopt.evaluation.stage0_finalizer import Stage0ReportArtifacts
from hcuopt.evaluation.stage0_protocol import load_registered_stage0_protocol
from hcuopt.evaluation.stage0_verifier import (
    Stage0ProbeEvidenceReference,
    Stage0VerificationContext,
    Stage0VerificationResult,
    verification_input_digest,
)
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


class ProtocolBoundFinalizerFixture:
    """Exercise repository wiring without treating zero-hash fixtures as raw evidence."""

    def verify(
        self,
        context: Stage0VerificationContext,
        references: tuple[Stage0ProbeEvidenceReference, ...],
        *,
        protocol_version: str,
    ) -> Stage0VerificationResult:
        protocol = load_registered_stage0_protocol(protocol_version)
        by_type = {reference.probe_type: reference for reference in references}
        return Stage0VerificationResult(
            protocol_version=protocol_version,
            protocol_hash=protocol.protocol_hash,
            input_digest=verification_input_digest(context, by_type, protocol),
            measurement=GateResult.PASS,
            profiler=ProfilerCapability.FULL,
            hot_patch=HotPatchCapability.HOT_PATCH,
            hardware_fingerprint="sha256:" + "a" * 64,
            software_fingerprint="sha256:" + "b" * 64,
            timer_resolution_ns=100.0,
            noise_sigma_ns=200.0,
            noise_cv=0.01,
            mde_ratio=0.03,
            statistics={},
            input_evidence=(),
            verifier_provenance=AdapterProvenance(
                profile="stage0-d-verifier",
                capability="stage0_independent_verification",
                adapter_name="ProtocolBoundFinalizerFixture",
                adapter_version="1",
                implementation_kind="real",
            ),
        )

    def publish_report(
        self,
        context: Stage0VerificationContext,
        verification: Stage0VerificationResult,
        *,
        mode: ProjectMode,
        reasons: tuple[str, ...],
        accepted_target_risks: tuple[str, ...],
    ) -> Stage0ReportArtifacts:
        del context, verification, mode, reasons, accepted_target_risks
        return Stage0ReportArtifacts(
            machine_report_uri="file:///fixture/stage0-report.json",
            machine_report_hash="sha256:" + "c" * 64,
            markdown_report_uri="file:///fixture/stage0-report.md",
            markdown_report_hash="sha256:" + "d" * 64,
        )


@unittest.skipUnless(DATABASE_URL and psycopg, "requires PostgreSQL and psycopg")
@pytest.mark.postgres
class Stage0ControlPlanePostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        assert DATABASE_URL is not None
        assert psycopg is not None
        self.repository = PostgresRepository(
            DATABASE_URL,
            stage0_finalizer=ProtocolBoundFinalizerFixture(),
        )
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
                protocol_version="s0-g0-v1",
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
                capabilities={"resource_id": "hcu-7"},
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
            self.assertEqual(job["payload"]["workload_id"], "sglang-qwen2.5-0.5b")
            self.assertEqual(job["payload"]["adapter_profile"], PROFILE)
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
                    "fence": {
                        "fenced": True,
                        "resource_id": "hcu-7",
                        "fencing_token": job["fencing_token"],
                    },
                    "health": {"healthy": True, "resource_id": "hcu-7"},
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
        unconfigured = PostgresRepository(DATABASE_URL)
        with self.assertRaisesRegex(Conflict, "verifier is not configured"):
            unconfigured.finalize_stage0_run(run["stage0_run_id"])
        with self.repository.connection() as connection:
            connection.execute(
                """
                UPDATE stage0_probe_records
                SET summary = '{"producer_summary":"must_be_ignored"}'::jsonb
                WHERE stage0_run_id = %s
                """,
                (run["stage0_run_id"],),
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
        self.assertEqual(report["protocol_version"], "s0-g0-v1")
        self.assertTrue(report["input_digest"].startswith("sha256:"))
        self.assertEqual(
            report["accepted_target_risks"], ["device_isolation_not_reserved"]
        )
        task = self.repository.get_task(run["task_id"])
        self.assertEqual(task["state"], TaskState.BASELINE_PENDING.value)

    def test_formal_creation_rejects_an_unregistered_protocol(self) -> None:
        with self.assertRaisesRegex(Conflict, "repository-registered protocol"):
            self.repository.create_stage0_run(
                Stage0RunCreate(
                    name="Unregistered Stage 0",
                    workload_id="sglang-qwen2.5-0.5b",
                    target_id=self.target.target_id,
                    adapter_profile=PROFILE,
                    mode=Stage0RunMode.FORMAL,
                    protocol_version="caller-invented-v1",
                    idempotency_key="unregistered-formal-stage0-fixture",
                ),
                self.target,
                str(TARGET_PATH),
            )

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

    def test_dry_run_hotpatch_gets_exclusive_lease_and_public_budget(self) -> None:
        request = Stage0RunCreate(
            name="Stage 0 dry-run lease fixture",
            workload_id="sglang-qwen2.5-0.5b",
            target_id=self.target.target_id,
            adapter_profile=PROFILE,
            mode=Stage0RunMode.DRY_RUN,
            protocol_version="s0-g0-v1",
            idempotency_key="dry-run-hotpatch-exclusive-fixture",
            budget={"max_wall_seconds": 123, "max_samples": 456},
        )
        run = self.repository.create_stage0_run(request, self.target, str(TARGET_PATH))

        jobs = self.repository.stage0_run_summary(run["stage0_run_id"])["jobs"]
        jobs_by_probe = {
            Stage0ProbeType(job["payload"]["probe_type"]): job for job in jobs
        }

        self.assertEqual(
            jobs_by_probe[Stage0ProbeType.HOTPATCH]["lease_scope"],
            LeaseScope.EXCLUSIVE.value,
        )
        for probe_type, job in jobs_by_probe.items():
            if probe_type is not Stage0ProbeType.HOTPATCH:
                self.assertEqual(job["lease_scope"], LeaseScope.NONE.value)
            self.assertEqual(
                job["payload"]["budget"],
                {"max_wall_seconds": 123, "max_samples": 456},
            )

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
