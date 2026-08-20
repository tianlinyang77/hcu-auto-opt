from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hcuopt.api.app import create_app
from hcuopt.contracts.platform_v1 import AdapterProvenance, TargetSpec
from hcuopt.domain.enums import (
    GateResult,
    HotPatchCapability,
    ProfilerCapability,
    ProjectMode,
    Stage0ProbeType,
    Stage0RunMode,
    Stage0RunState,
    TaskState,
)
from hcuopt.domain.models import Stage0Report
from hcuopt.evaluation.stage0_inputs import (
    Stage0FinalizationProbeSnapshot,
    Stage0FinalizationRunSnapshot,
    Stage0FinalizationSnapshot,
    Stage0FinalizationTargetSnapshot,
    Stage0FinalizationTaskSnapshot,
    stage0_snapshot_digest,
)
from hcuopt.evaluation.stage0_protocol import (
    LoadedStage0Protocol,
    Stage0ProtocolError,
    load_registered_stage0_protocol,
)
from hcuopt.evaluation.stage0_reporting import (
    Stage0ReportArtifact,
    Stage0ReportArtifacts,
)
from hcuopt.evaluation.stage0_verifier import (
    Stage0EvidenceError,
    Stage0VerificationContext,
    Stage0VerificationResult,
    target_fingerprint,
)
from hcuopt.orchestrator.stage0_finalization import Stage0FinalizationService

ROOT = Path(__file__).resolve().parents[2]
TARGET_PATH = ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml"

TASK_ID = UUID(int=9101)
RUN_ID = UUID(int=9102)
TARGET_SNAPSHOT_ID = UUID(int=9103)
LEASE_ID = UUID(int=9104)
OTHER_TASK_ID = UUID(int=9199)
RESOURCE_ID = "hcu-7"
FENCING_TOKEN = 23
WORKLOAD_ID = "stage0-short-kernel-v1"
PROFILE = "nmz36-stage0-composite-v1"
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64

PROVENANCE = AdapterProvenance(
    profile=PROFILE,
    capability="stage0_probe",
    adapter_name="CompositeStage0Adapter",
    adapter_version="2",
    implementation_kind="real",
    source_commit="1" * 40,
)
VERIFIER_PROVENANCE = AdapterProvenance(
    profile="stage0-d-verifier",
    capability="stage0_independent_verification",
    adapter_name="Stage0Verifier",
    adapter_version="1",
    implementation_kind="real",
    source_commit="2" * 40,
)
HEALTHY_CLEANUP = {
    "fence": {
        "fenced": True,
        "resource_id": RESOURCE_ID,
        "fencing_token": FENCING_TOKEN,
    },
    "health": {
        "healthy": True,
        "resource_id": RESOURCE_ID,
        "remaining_processes": [],
    },
}


class _RecordingRepository:
    def __init__(
        self,
        snapshot: Stage0FinalizationSnapshot,
        events: list[str],
    ) -> None:
        self.snapshot = snapshot
        self.events = events
        self.commit_call: dict[str, Any] | None = None
        self.fail_call: dict[str, Any] | None = None

    def load_stage0_finalization_snapshot(
        self,
        stage0_run_id: UUID,
    ) -> Stage0FinalizationSnapshot:
        assert stage0_run_id == RUN_ID
        self.events.append("load")
        return self.snapshot

    def commit_stage0_finalization(
        self,
        stage0_run_id: UUID,
        *,
        expected_snapshot_digest: str,
        verification: Stage0VerificationResult,
        report: dict[str, Any],
    ) -> dict[str, Any]:
        assert stage0_run_id == RUN_ID
        self.events.append("commit")
        self.commit_call = {
            "expected_snapshot_digest": expected_snapshot_digest,
            "verification": verification,
            "report": report,
        }
        return report

    def fail_stage0_finalization(
        self,
        stage0_run_id: UUID,
        *,
        expected_snapshot_digest: str,
        error_code: str,
        message: str,
    ) -> dict[str, Any]:
        assert stage0_run_id == RUN_ID
        self.events.append("fail")
        self.fail_call = {
            "expected_snapshot_digest": expected_snapshot_digest,
            "error_code": error_code,
            "message": message,
        }
        return {"state": "failed", "error_code": error_code}


class _RecordingVerifier:
    def __init__(
        self,
        events: list[str],
        result: Stage0VerificationResult,
        error: Stage0EvidenceError | None,
    ) -> None:
        self.events = events
        self.result = result
        self.error = error

    def verify(self, *_: object) -> Stage0VerificationResult:
        self.events.append("verify")
        if self.error is not None:
            raise self.error
        return self.result


def _load_target() -> TargetSpec:
    return TargetSpec.model_validate(yaml.safe_load(TARGET_PATH.read_text(encoding="utf-8")))


def _snapshot(
    results_root: Path,
    *,
    mode: Stage0RunMode = Stage0RunMode.FORMAL,
    state: Stage0RunState = Stage0RunState.READY,
    report: dict[str, Any] | None = None,
    wrong_path_probe: Stage0ProbeType | None = None,
    wrong_task_probe: Stage0ProbeType | None = None,
) -> Stage0FinalizationSnapshot:
    target = _load_target()
    run_root = results_root / str(RUN_ID)
    probes = tuple(
        Stage0FinalizationProbeSnapshot(
            probe_record_id=UUID(int=9200 + ordinal),
            task_id=OTHER_TASK_ID if probe_type is wrong_task_probe else TASK_ID,
            target_snapshot_id=TARGET_SNAPSHOT_ID,
            probe_type=probe_type,
            protocol_version="s0-g0-v1",
            raw_evidence_uri=(
                (run_root / "wrong" / "raw.json").as_uri()
                if probe_type is wrong_path_probe
                else (run_root / probe_type.value / "raw.json").as_uri()
            ),
            raw_evidence_hash="sha256:" + f"{ordinal + 1:x}" * 64,
            adapter_provenance=(PROVENANCE,),
            synthetic=False,
            lease_id=LEASE_ID,
            resource_id=RESOURCE_ID,
            fencing_token=FENCING_TOKEN,
            cleanup_evidence=HEALTHY_CLEANUP,
        )
        for ordinal, probe_type in enumerate(Stage0ProbeType)
    )
    return Stage0FinalizationSnapshot(
        run=Stage0FinalizationRunSnapshot(
            stage0_run_id=RUN_ID,
            task_id=TASK_ID,
            target_snapshot_id=TARGET_SNAPSHOT_ID,
            adapter_profile=PROFILE,
            mode=mode,
            state=state,
            protocol_version="s0-g0-v1",
            report=report,
        ),
        task=Stage0FinalizationTaskSnapshot(
            task_id=TASK_ID,
            state=TaskState.STAGE0_PENDING,
            workload_id=WORKLOAD_ID,
            adapter_profile=PROFILE,
            stage0_authority="none",
        ),
        target=Stage0FinalizationTargetSnapshot(
            target_snapshot_id=TARGET_SNAPSHOT_ID,
            target_id=target.target_id,
            target_fingerprint=target_fingerprint(target),
            specification=target,
        ),
        probes=probes,
    )


def _verification(
    *,
    measurement: GateResult = GateResult.PASS,
    profiler: ProfilerCapability = ProfilerCapability.FULL,
    hot_patch: HotPatchCapability = HotPatchCapability.HOT_PATCH,
) -> Stage0VerificationResult:
    failure_codes = () if measurement is GateResult.PASS else ("noise_cv_exceeded",)
    reasons = () if measurement is GateResult.PASS else ("noise CV exceeds protocol",)
    return Stage0VerificationResult(
        protocol_version="s0-g0-v1",
        protocol_hash=SHA_A,
        input_digest=SHA_B,
        measurement=measurement,
        profiler=profiler,
        hot_patch=hot_patch,
        hardware_fingerprint=SHA_B,
        software_fingerprint=SHA_C,
        timer_resolution_ns=1.0,
        noise_sigma_ns=2.0,
        noise_cv=0.002,
        mde_ratio=0.003,
        failure_codes=failure_codes,
        reasons=reasons,
        statistics={"noise": {"cv": 0.002}},
        input_evidence=tuple(
            {
                "probe_record_id": str(UUID(int=9200 + ordinal)),
                "probe_type": probe_type.value,
                "uri": f"file:///raw/{probe_type.value}.json",
                "sha256": "sha256:" + f"{ordinal + 1:x}" * 64,
            }
            for ordinal, probe_type in enumerate(Stage0ProbeType)
        ),
        verifier_provenance=VERIFIER_PROVENANCE,
    )


def _artifacts(run_root: Path) -> Stage0ReportArtifacts:
    report_root = run_root / "verification"
    return Stage0ReportArtifacts(
        verification_json=Stage0ReportArtifact(
            uri=(report_root / "stage0-verification.json").as_uri(),
            sha256=SHA_A,
            byte_count=10,
        ),
        verification_markdown=Stage0ReportArtifact(
            uri=(report_root / "stage0-verification.md").as_uri(),
            sha256=SHA_B,
            byte_count=11,
        ),
        sha256sums=Stage0ReportArtifact(
            uri=(report_root / "sha256sums.json").as_uri(),
            sha256=SHA_C,
            byte_count=12,
        ),
    )


def _service(
    snapshot: Stage0FinalizationSnapshot,
    results_root: Path,
    result: Stage0VerificationResult,
    *,
    verifier_error: Stage0EvidenceError | None = None,
    report_error: Exception | None = None,
) -> tuple[Stage0FinalizationService, _RecordingRepository, list[str]]:
    events: list[str] = []
    repository = _RecordingRepository(snapshot, events)
    protocol = load_registered_stage0_protocol("s0-g0-v1")

    def protocol_loader(version: str) -> LoadedStage0Protocol:
        assert version == "s0-g0-v1"
        events.append("protocol")
        return protocol

    def reader_factory(run_root: Path) -> object:
        assert run_root == results_root / str(RUN_ID)
        events.append("reader")
        return object()

    def verifier_factory(
        loaded_protocol: LoadedStage0Protocol,
        _reader: object,
    ) -> _RecordingVerifier:
        assert loaded_protocol == protocol
        events.append("verifier_factory")
        return _RecordingVerifier(events, result, verifier_error)

    def report_writer(
        run_root: Path,
        _context: Stage0VerificationContext,
        verification: Stage0VerificationResult,
        _evaluation: Stage0Report,
    ) -> Stage0ReportArtifacts:
        assert verification is result
        events.append("report")
        if report_error is not None:
            raise report_error
        return _artifacts(run_root)

    service = Stage0FinalizationService(
        repository,
        results_root,
        protocol_loader=protocol_loader,
        reader_factory=reader_factory,
        verifier_factory=verifier_factory,
        report_writer=report_writer,
    )
    return service, repository, events


@pytest.mark.parametrize(
    ("result", "expected_mode"),
    [
        (
            _verification(measurement=GateResult.FAIL),
            ProjectMode.STOPPED_MEASUREMENT,
        ),
        (
            _verification(profiler=ProfilerCapability.NONE),
            ProjectMode.CONFIG_ONLY,
        ),
        (
            _verification(profiler=ProfilerCapability.DEGRADED),
            ProjectMode.DEGRADED_MANUAL_INTAKE,
        ),
        (
            _verification(),
            ProjectMode.FULL_MVP,
        ),
    ],
)
def test_finalize_recomputes_four_project_modes_and_commits_after_report(
    tmp_path: Path,
    result: Stage0VerificationResult,
    expected_mode: ProjectMode,
) -> None:
    results_root = (tmp_path / "results" / "stage0").absolute()
    snapshot = _snapshot(results_root)
    service, repository, events = _service(snapshot, results_root, result)

    report = service.finalize(RUN_ID)

    assert report["mode"] == expected_mode.value
    assert report["automatic_release_allowed"] is False
    assert report["evidence_authority"] == "formal"
    assert events == [
        "load",
        "protocol",
        "reader",
        "verifier_factory",
        "verify",
        "report",
        "commit",
    ]
    assert repository.commit_call == {
        "expected_snapshot_digest": stage0_snapshot_digest(snapshot),
        "verification": result,
        "report": report,
    }
    assert repository.fail_call is None


def test_invalid_evidence_marks_run_failed_without_committing(tmp_path: Path) -> None:
    results_root = (tmp_path / "results" / "stage0").absolute()
    snapshot = _snapshot(results_root)
    error = Stage0EvidenceError("evidence_hash_mismatch", "raw hash changed")
    service, repository, events = _service(
        snapshot,
        results_root,
        _verification(),
        verifier_error=error,
    )

    with pytest.raises(Stage0EvidenceError) as captured:
        service.finalize(RUN_ID)

    assert captured.value is error
    assert events == [
        "load",
        "protocol",
        "reader",
        "verifier_factory",
        "verify",
        "fail",
    ]
    assert repository.fail_call == {
        "expected_snapshot_digest": stage0_snapshot_digest(snapshot),
        "error_code": "evidence_hash_mismatch",
        "message": "raw hash changed",
    }
    assert repository.commit_call is None


def test_dry_run_cannot_gain_formal_authority(tmp_path: Path) -> None:
    results_root = (tmp_path / "results" / "stage0").absolute()
    snapshot = _snapshot(results_root, mode=Stage0RunMode.DRY_RUN)
    service, repository, events = _service(snapshot, results_root, _verification())

    with pytest.raises(Stage0EvidenceError) as captured:
        service.finalize(RUN_ID)

    assert captured.value.code == "dry_run_not_formal"
    assert events == ["load", "fail"]
    assert repository.fail_call == {
        "expected_snapshot_digest": stage0_snapshot_digest(snapshot),
        "error_code": "dry_run_not_formal",
        "message": "Dry Run evidence cannot authorize formal Stage 0",
    }
    assert repository.commit_call is None


def test_report_storage_error_leaves_ready_run_retryable(tmp_path: Path) -> None:
    results_root = (tmp_path / "results" / "stage0").absolute()
    snapshot = _snapshot(results_root)
    storage_error = OSError("report disk unavailable")
    service, repository, events = _service(
        snapshot,
        results_root,
        _verification(),
        report_error=storage_error,
    )

    with pytest.raises(OSError) as captured:
        service.finalize(RUN_ID)

    assert captured.value is storage_error
    assert snapshot.run.state is Stage0RunState.READY
    assert events == [
        "load",
        "protocol",
        "reader",
        "verifier_factory",
        "verify",
        "report",
    ]
    assert repository.fail_call is None
    assert repository.commit_call is None


def test_protocol_registry_failure_is_infrastructure_and_does_not_fail_run(
    tmp_path: Path,
) -> None:
    results_root = (tmp_path / "results" / "stage0").absolute()
    snapshot = _snapshot(results_root)
    events: list[str] = []
    repository = _RecordingRepository(snapshot, events)

    def unavailable_protocol(_version: str) -> LoadedStage0Protocol:
        raise Stage0ProtocolError("installed protocol file is temporarily unavailable")

    service = Stage0FinalizationService(
        repository,
        results_root,
        protocol_loader=unavailable_protocol,
    )

    with pytest.raises(Stage0ProtocolError, match="temporarily unavailable"):
        service.finalize(RUN_ID)

    assert events == ["load"]
    assert repository.fail_call is None
    assert repository.commit_call is None


def test_stage0_evidence_error_is_exposed_as_stable_http_422(tmp_path: Path) -> None:
    class RejectingRepository:
        def migrate(self) -> None:
            return None

        def load_stage0_finalization_snapshot(
            self, _stage0_run_id: UUID
        ) -> Stage0FinalizationSnapshot:
            raise Stage0EvidenceError("evidence_hash_mismatch", "raw hash changed")

    application = create_app(
        repository=RejectingRepository(),  # type: ignore[arg-type]
        stage0_results_root=tmp_path,
    )
    with TestClient(application) as client:
        response = client.post(f"/v1/stage0-runs/{RUN_ID}/finalize")

    assert response.status_code == 422
    assert response.json() == {
        "code": "evidence_hash_mismatch",
        "message": "raw hash changed",
        "retryable": False,
    }


def test_finalized_replay_returns_persisted_report_without_reading_evidence(
    tmp_path: Path,
) -> None:
    results_root = (tmp_path / "results" / "stage0").absolute()
    persisted = {
        "mode": ProjectMode.FULL_MVP.value,
        "automatic_release_allowed": False,
    }
    snapshot = _snapshot(
        results_root,
        state=Stage0RunState.FINALIZED,
        report=persisted,
    )
    service, repository, events = _service(snapshot, results_root, _verification())

    assert service.finalize(RUN_ID) == persisted
    assert events == ["load"]
    assert repository.fail_call is None
    assert repository.commit_call is None


@pytest.mark.parametrize(
    ("snapshot_factory", "expected_code"),
    [
        (
            lambda root: _snapshot(root, wrong_path_probe=Stage0ProbeType.NOISE),
            "evidence_layout_mismatch",
        ),
        (
            lambda root: _snapshot(root, wrong_task_probe=Stage0ProbeType.PROFILER),
            "control_plane_binding_mismatch",
        ),
    ],
)
def test_layout_and_control_binding_mismatches_fail_before_evidence_read(
    tmp_path: Path,
    snapshot_factory: Callable[[Path], Stage0FinalizationSnapshot],
    expected_code: str,
) -> None:
    results_root = (tmp_path / "results" / "stage0").absolute()
    snapshot = snapshot_factory(results_root)
    service, repository, events = _service(snapshot, results_root, _verification())

    with pytest.raises(Stage0EvidenceError) as captured:
        service.finalize(RUN_ID)

    assert captured.value.code == expected_code
    assert events == ["load", "protocol", "fail"]
    assert repository.fail_call is not None
    assert repository.fail_call["error_code"] == expected_code
    assert repository.commit_call is None


def test_snapshot_rejects_producer_summary_and_digest_ignores_probe_order(
    tmp_path: Path,
) -> None:
    results_root = (tmp_path / "results" / "stage0").absolute()
    snapshot = _snapshot(results_root)
    reordered = snapshot.model_copy(update={"probes": tuple(reversed(snapshot.probes))})
    payload: dict[str, Any] = snapshot.model_dump(mode="python")
    payload["probes"][0]["summary"] = {"cv": 0.0, "measurement_passed": True}

    assert stage0_snapshot_digest(reordered) == stage0_snapshot_digest(snapshot)
    with pytest.raises(ValidationError, match="summary"):
        Stage0FinalizationSnapshot.model_validate(payload)
