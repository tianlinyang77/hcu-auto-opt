import os
import tempfile
import threading
import unittest
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.contracts.v1 import Stage0ProbeResult, Stage0RunCreate, WorkerRegister
from hcuopt.domain.enums import (
    GateResult,
    HotPatchCapability,
    ProfilerCapability,
    ProjectMode,
    Stage0ProbeType,
    Stage0RunMode,
    Stage0RunState,
    TaskState,
    WorkerType,
)
from hcuopt.domain.errors import Conflict
from hcuopt.evaluation.stage0_inputs import (
    build_verification_inputs,
    stage0_snapshot_digest,
)
from hcuopt.evaluation.stage0_protocol import load_registered_stage0_protocol
from hcuopt.evaluation.stage0_verifier import (
    Stage0EvidenceError,
    Stage0VerificationResult,
    Stage0Verifier,
    verification_input_digest,
)
from hcuopt.orchestrator.stage0 import Stage0Coordinator
from hcuopt.orchestrator.stage0_finalization import Stage0FinalizationService
from hcuopt.stage0 import evaluate_stage0
from hcuopt.storage.repository import PostgresRepository
from hcuopt.targets import load_target
from tests.stage0_v2_fixtures import write_formal_stage0_raw_probe

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
                protocol_version="s0-g0-v1",
                idempotency_key=key,
            ),
            self.target,
            str(TARGET_PATH),
        )

    def _record_all_formal_probes(
        self, key: str, *, results_root: Path | None = None
    ) -> dict:
        run = self._create(key, Stage0RunMode.FORMAL)
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
            source_commit="1" * 40,
        )
        protocol = load_registered_stage0_protocol(run["protocol_version"])

        for _ in range(len(Stage0ProbeType)):
            job = self.repository.claim_job("stage0-gpu")
            assert job is not None
            probe_type = Stage0ProbeType(job["payload"]["probe_type"])
            artifact = (
                write_formal_stage0_raw_probe(
                    results_root
                    / str(run["stage0_run_id"])
                    / probe_type.value
                    / "raw.json",
                    probe_type,
                    task_id=run["task_id"],
                    stage0_run_id=run["stage0_run_id"],
                    target_snapshot_id=run["target_snapshot_id"],
                    target=self.target,
                    workload_id="sglang-qwen2.5-0.5b",
                    protocol=protocol,
                    lease_id=UUID(str(job["lease_id"])),
                    resource_id=str(job["resource_id"]),
                    fencing_token=int(job["fencing_token"]),
                    provenance=provenance,
                )
                if results_root is not None
                else None
            )
            result = Stage0ProbeResult(
                stage0_run_id=run["stage0_run_id"],
                target_snapshot_id=run["target_snapshot_id"],
                probe_type=probe_type,
                protocol_version=run["protocol_version"],
                raw_evidence_uri=(
                    artifact.uri
                    if artifact is not None
                    else f"file:///stage0/{probe_type.value}.json"
                ),
                raw_evidence_hash=(
                    artifact.sha256 if artifact is not None else "sha256:" + "0" * 64
                ),
                summary=self._summary(probe_type),
                adapter_provenance=[provenance],
                synthetic=False,
                cleanup_evidence={
                    "fence": {
                        "fenced": True,
                        "resource_id": job["resource_id"],
                        "fencing_token": job["fencing_token"],
                    },
                    "health": {
                        "healthy": True,
                        "resource_id": job["resource_id"],
                        "remaining_processes": [],
                    },
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
        return run

    def _verification_and_report(
        self,
        run: dict,
        *,
        measurement: GateResult = GateResult.PASS,
        profiler: ProfilerCapability = ProfilerCapability.FULL,
        hot_patch: HotPatchCapability = HotPatchCapability.HOT_PATCH,
    ) -> tuple[str, Stage0VerificationResult, dict]:
        snapshot = self.repository.load_stage0_finalization_snapshot(
            run["stage0_run_id"]
        )
        protocol = load_registered_stage0_protocol(snapshot.run.protocol_version)
        context, references = build_verification_inputs(snapshot, protocol)
        input_digest = verification_input_digest(context, references, protocol)
        verification = Stage0VerificationResult(
            protocol_version=protocol.protocol.protocol_version,
            protocol_hash=protocol.protocol_hash,
            input_digest=input_digest,
            measurement=measurement,
            profiler=profiler,
            hot_patch=hot_patch,
            hardware_fingerprint="sha256:" + "1" * 64,
            software_fingerprint="sha256:" + "2" * 64,
            timer_resolution_ns=1.0,
            noise_sigma_ns=2.0,
            noise_cv=0.01,
            mde_ratio=0.02,
            statistics={"source": "postgres-fixture"},
            input_evidence=tuple(
                {
                    "probe_record_id": str(reference.probe_record_id),
                    "probe_type": probe_type.value,
                    "uri": reference.raw_evidence_uri,
                    "sha256": reference.raw_evidence_hash,
                }
                for probe_type, reference in sorted(
                    references.items(), key=lambda item: item[0].value
                )
            ),
            verifier_provenance=Stage0Verifier.provenance,
        )
        decision = evaluate_stage0(
            verification.to_stage0_evidence(
                evidence_uri="file:///stage0/report/stage0-verification.json"
            )
        )
        report = {
            "task_id": str(run["task_id"]),
            "mode": decision.mode.value,
            "reasons": list(decision.reasons),
            "automatic_release_allowed": False,
            "evidence_authority": "formal",
            "protocol_version": verification.protocol_version,
            "protocol_hash": verification.protocol_hash,
            "input_digest": verification.input_digest,
            "measurement_gate": verification.measurement.value,
            "profiler_gate": verification.profiler.value,
            "hotpatch_gate": verification.hot_patch.value,
            "failure_codes": [],
            "json_report_uri": "file:///stage0/report/stage0-verification.json",
            "json_report_hash": "sha256:" + "3" * 64,
            "markdown_report_uri": "file:///stage0/report/stage0-verification.md",
            "markdown_report_hash": "sha256:" + "4" * 64,
            "manifest_uri": "file:///stage0/report/sha256sums.json",
            "manifest_hash": "sha256:" + "5" * 64,
        }
        return stage0_snapshot_digest(snapshot), verification, report

    def test_formal_probe_jobs_open_barrier_and_finalize_idempotently(self) -> None:
        run = self._record_all_formal_probes("formal-stage0-fixture")
        snapshot_digest, verification, report = self._verification_and_report(run)

        barrier = threading.Barrier(2)
        reports: list[dict] = []
        errors: list[BaseException] = []

        def finalize() -> None:
            try:
                barrier.wait()
                reports.append(
                    self.repository.commit_stage0_finalization(
                        run["stage0_run_id"],
                        expected_snapshot_digest=snapshot_digest,
                        verification=verification,
                        report=report,
                    )
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
        persisted_report = reports[0]
        replay = self.repository.commit_stage0_finalization(
            run["stage0_run_id"],
            expected_snapshot_digest=snapshot_digest,
            verification=verification,
            report=report,
        )
        self.assertEqual(persisted_report, replay)
        self.assertEqual(persisted_report["mode"], ProjectMode.FULL_MVP.value)
        self.assertFalse(persisted_report["automatic_release_allowed"])
        task = self.repository.get_task(run["task_id"])
        self.assertEqual(task["state"], TaskState.BASELINE_PENDING.value)

    def test_summary_tampering_is_not_a_finalization_input(self) -> None:
        run = self._record_all_formal_probes("summary-tamper-fixture")
        before = self.repository.load_stage0_finalization_snapshot(
            run["stage0_run_id"]
        )
        before_digest = stage0_snapshot_digest(before)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE stage0_probe_records
                SET summary = '{"gate_result":"fail","noise_cv":999,"capability":"none"}'::jsonb
                WHERE stage0_run_id = %s
                """,
                (run["stage0_run_id"],),
            )
        self.connection.commit()
        after = self.repository.load_stage0_finalization_snapshot(run["stage0_run_id"])
        self.assertEqual(before_digest, stage0_snapshot_digest(after))

        snapshot_digest, verification, report = self._verification_and_report(run)
        persisted = self.repository.commit_stage0_finalization(
            run["stage0_run_id"],
            expected_snapshot_digest=snapshot_digest,
            verification=verification,
            report=report,
        )
        self.assertEqual(persisted["mode"], ProjectMode.FULL_MVP.value)

    @unittest.skipUnless(os.name == "posix", "Formal reader requires POSIX openat")
    def test_real_raw_verifier_report_and_postgres_commit_ignore_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            results_root = Path(temporary_directory) / "results" / "stage0"
            run = self._record_all_formal_probes(
                "real-verifier-finalize-fixture",
                results_root=results_root,
            )

            class PreviewRepository:
                def __init__(self, repository: PostgresRepository) -> None:
                    self.repository = repository

                def load_stage0_finalization_snapshot(self, stage0_run_id: UUID):
                    return self.repository.load_stage0_finalization_snapshot(
                        stage0_run_id
                    )

                def commit_stage0_finalization(
                    self,
                    _stage0_run_id: UUID,
                    *,
                    expected_snapshot_digest: str,
                    verification: Stage0VerificationResult,
                    report: dict,
                ) -> dict:
                    self.assertion_payload = (
                        expected_snapshot_digest,
                        verification.input_digest,
                    )
                    return report

                def fail_stage0_finalization(self, *_args, **_kwargs):
                    raise AssertionError("valid raw evidence must not fail")

            preview_repository = PreviewRepository(self.repository)
            preview = Stage0FinalizationService(
                preview_repository,  # type: ignore[arg-type]
                results_root,
            ).finalize(run["stage0_run_id"])
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE stage0_probe_records
                    SET summary = '{"gate_result":"fail","noise_cv":999,"capability":"none"}'::jsonb
                    WHERE stage0_run_id = %s
                    """,
                    (run["stage0_run_id"],),
                )
            self.connection.commit()

            persisted = Stage0FinalizationService(
                self.repository,
                results_root,
            ).finalize(run["stage0_run_id"])

            self.assertEqual(preview, persisted)
            self.assertEqual(persisted["mode"], ProjectMode.FULL_MVP.value)
            self.assertEqual(persisted["measurement_gate"], "pass")
            self.assertEqual(persisted["profiler_gate"], "full")
            self.assertEqual(persisted["hotpatch_gate"], "hot_patch")
            for name in (
                "json_report_hash",
                "markdown_report_hash",
                "manifest_hash",
            ):
                self.assertTrue(persisted[name].startswith("sha256:"))
            task = self.repository.get_task(run["task_id"])
            self.assertEqual(task["state"], TaskState.BASELINE_PENDING.value)
            self.assertEqual(task["stage0_authority"], "formal")

    def test_invalid_evidence_failure_does_not_grant_task_authority(self) -> None:
        run = self._record_all_formal_probes("invalid-evidence-fixture")
        snapshot = self.repository.load_stage0_finalization_snapshot(
            run["stage0_run_id"]
        )
        self.repository.fail_stage0_finalization(
            run["stage0_run_id"],
            expected_snapshot_digest=stage0_snapshot_digest(snapshot),
            error_code="evidence_hash_mismatch",
            message="fixture hash mismatch",
        )
        failed = self.repository.get_stage0_run(run["stage0_run_id"])
        self.assertEqual(failed["state"], Stage0RunState.FAILED.value)
        task = self.repository.get_task(run["task_id"])
        self.assertEqual(task["state"], TaskState.STAGE0_PENDING.value)
        self.assertEqual(task["stage0_authority"], "none")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM stage0_evidence WHERE task_id = %s",
                (run["task_id"],),
            )
            count = cursor.fetchone()[0]
        self.assertEqual(count, 0)

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
        with self.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE stage0_runs SET state = 'ready' WHERE stage0_run_id = %s",
                (run["stage0_run_id"],),
            )
        self.connection.commit()
        with self.assertRaisesRegex(Stage0EvidenceError, "Dry Run evidence"):
            Stage0FinalizationService(self.repository, ROOT / "results" / "stage0").finalize(
                run["stage0_run_id"]
            )
        failed = self.repository.get_stage0_run(run["stage0_run_id"])
        self.assertEqual(failed["state"], Stage0RunState.FAILED.value)
        task = self.repository.get_task(run["task_id"])
        self.assertEqual(task["state"], TaskState.STAGE0_PENDING.value)
        self.assertEqual(task["stage0_authority"], "none")
