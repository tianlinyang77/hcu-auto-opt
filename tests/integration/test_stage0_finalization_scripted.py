from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from hcuopt.contracts.platform_v1 import AdapterProvenance, TargetSpec
from hcuopt.domain.enums import (
    ProjectMode,
    Stage0ProbeType,
    Stage0RunMode,
    Stage0RunState,
    TaskState,
)
from hcuopt.evaluation.stage0_inputs import (
    Stage0FinalizationProbeSnapshot,
    Stage0FinalizationRunSnapshot,
    Stage0FinalizationSnapshot,
    Stage0FinalizationTargetSnapshot,
    Stage0FinalizationTaskSnapshot,
)
from hcuopt.evaluation.stage0_protocol import load_registered_stage0_protocol
from hcuopt.evaluation.stage0_reporting import STAGE0_REPORT_DISCLAIMER
from hcuopt.evaluation.stage0_verifier import Stage0VerificationResult, target_fingerprint
from hcuopt.orchestrator.stage0_finalization import Stage0FinalizationService
from tests.stage0_v2_fixtures import write_formal_stage0_raw_suite

ROOT = Path(__file__).resolve().parents[2]
TARGET_PATH = ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml"
TASK_ID = UUID(int=51_001)
RUN_ID = UUID(int=51_002)
TARGET_SNAPSHOT_ID = UUID(int=51_003)
LEASE_ID = UUID(int=51_004)
RESOURCE_ID = "hcu-7"
FENCING_TOKEN = 31
WORKLOAD_ID = "stage0-short-kernel-v1"
PROFILE = "nmz36-stage0-composite-v1"
PROVENANCE = AdapterProvenance(
    profile=PROFILE,
    capability="stage0_probe",
    adapter_name="ScriptedCompositeStage0Adapter",
    adapter_version="1",
    implementation_kind="real",
    source_commit="1" * 40,
)


class _Repository:
    def __init__(self, snapshot: Stage0FinalizationSnapshot) -> None:
        self.snapshot = snapshot
        self.verification: Stage0VerificationResult | None = None
        self.report: dict[str, Any] | None = None

    def load_stage0_finalization_snapshot(
        self, _stage0_run_id: UUID
    ) -> Stage0FinalizationSnapshot:
        return self.snapshot

    def commit_stage0_finalization(
        self,
        _stage0_run_id: UUID,
        *,
        expected_snapshot_digest: str,
        verification: Stage0VerificationResult,
        report: dict[str, Any],
    ) -> dict[str, Any]:
        assert expected_snapshot_digest.startswith("sha256:")
        self.verification = verification
        self.report = report
        return report

    def fail_stage0_finalization(self, *_: object, **__: object) -> dict[str, Any]:
        raise AssertionError("passing scripted evidence must not enter the failure path")


@pytest.mark.skipif(
    os.name != "posix",
    reason="Formal EvidenceReader deliberately requires POSIX openat/O_NOFOLLOW",
)
def test_seven_raw_probes_recompute_and_publish_a_complete_formal_report(
    tmp_path: Path,
) -> None:
    target = TargetSpec.model_validate(yaml.safe_load(TARGET_PATH.read_text(encoding="utf-8")))
    protocol = load_registered_stage0_protocol("s0-g0-v1")
    results_root = (tmp_path / "results" / "stage0").absolute()
    run_root = results_root / str(RUN_ID)
    artifacts = write_formal_stage0_raw_suite(
        run_root,
        task_id=TASK_ID,
        stage0_run_id=RUN_ID,
        target_snapshot_id=TARGET_SNAPSHOT_ID,
        target=target,
        workload_id=WORKLOAD_ID,
        protocol=protocol,
        lease_id=LEASE_ID,
        resource_id=RESOURCE_ID,
        fencing_token=FENCING_TOKEN,
        provenance=PROVENANCE,
    )
    cleanup = {
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
    snapshot = Stage0FinalizationSnapshot(
        run=Stage0FinalizationRunSnapshot(
            stage0_run_id=RUN_ID,
            task_id=TASK_ID,
            target_snapshot_id=TARGET_SNAPSHOT_ID,
            adapter_profile=PROFILE,
            mode=Stage0RunMode.FORMAL,
            state=Stage0RunState.READY,
            protocol_version="s0-g0-v1",
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
        probes=tuple(
            Stage0FinalizationProbeSnapshot(
                probe_record_id=UUID(int=51_100 + ordinal),
                task_id=TASK_ID,
                target_snapshot_id=TARGET_SNAPSHOT_ID,
                probe_type=probe_type,
                protocol_version="s0-g0-v1",
                raw_evidence_uri=artifacts[probe_type].uri,
                raw_evidence_hash=artifacts[probe_type].sha256,
                adapter_provenance=(PROVENANCE,),
                synthetic=False,
                lease_id=LEASE_ID,
                resource_id=RESOURCE_ID,
                fencing_token=FENCING_TOKEN,
                cleanup_evidence=cleanup,
            )
            for ordinal, probe_type in enumerate(Stage0ProbeType)
        ),
    )
    repository = _Repository(snapshot)

    report = Stage0FinalizationService(repository, results_root).finalize(RUN_ID)

    assert report["mode"] == ProjectMode.FULL_MVP.value
    assert report["evidence_authority"] == "formal"
    assert report["automatic_release_allowed"] is False
    assert repository.verification is not None
    assert repository.verification.measurement.value == "pass"
    assert repository.verification.profiler.value == "full"
    assert repository.verification.hot_patch.value == "hot_patch"
    report_path = run_root / "verification" / "stage0-verification.json"
    markdown_path = run_root / "verification" / "stage0-verification.md"
    manifest_path = run_root / "verification" / "sha256sums.json"
    assert report_path.is_file()
    assert markdown_path.is_file()
    assert manifest_path.is_file()
    assert STAGE0_REPORT_DISCLAIMER in markdown_path.read_text(encoding="utf-8")
    assert report["json_report_hash"] == _sha256(report_path.read_bytes())
    assert report["markdown_report_hash"] == _sha256(markdown_path.read_bytes())
    assert report["manifest_hash"] == _sha256(manifest_path.read_bytes())
    manifest = json.loads(manifest_path.read_bytes())
    assert manifest["files"]["stage0-verification.json"] == report["json_report_hash"]
    assert manifest["files"]["stage0-verification.md"] == report["markdown_report_hash"]


def _sha256(encoded: bytes) -> str:
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
