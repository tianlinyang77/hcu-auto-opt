from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from hcuopt.adapters.fake import FakeExecutionAdapter, FakeSourceManager
from hcuopt.cli import main
from hcuopt.contracts.platform_v1 import (
    PLATFORM_CONTRACT_VERSION,
    AdapterProvenance,
    ArtifactManifest,
    EvaluationRun,
    EvidenceBundle,
    ExecutionRequest,
    ExecutionResult,
    MeasurementSeries,
    TargetSpec,
)
from hcuopt.domain.enums import LeaseScope
from hcuopt.domain.errors import TargetConfigError
from hcuopt.targets import TargetCatalog, load_target

ROOT = Path(__file__).parents[2]
TARGET_PATH = ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml"
FAKE_EXECUTOR_PROVENANCE = AdapterProvenance(
    profile="fake-v1-control-flow-only",
    capability="executor",
    adapter_name="FakeExecutionAdapter",
    adapter_version="1",
    implementation_kind="fake",
)


def test_locked_target_loads_as_platform_v1() -> None:
    target = load_target(TARGET_PATH)
    assert PLATFORM_CONTRACT_VERSION == "platform-v1.1"
    assert target.target_id == "nmz36-sglang-0.5.12"
    assert target.inference_image.python_version == "3.10"
    assert target.inference_image.immutable_reference.endswith(
        "@sha256:ee8eb5a76e9a4060ef2ffcbb9fa0da09aed2132c35592d1e723454a770dd38db"
    )
    assert target.source_baseline.commit == "dad582f28458cd0e11e0be675fbe7fcc7ab65ac1"


def test_target_rejects_tag_only_or_mismatched_immutable_image() -> None:
    raw = yaml.safe_load(TARGET_PATH.read_text(encoding="utf-8"))
    raw["inference_image"]["immutable_reference"] = (
        f"{raw['inference_image']['registry']}/{raw['inference_image']['repository']}:"
        f"{raw['inference_image']['tag']}"
    )
    with pytest.raises(ValidationError, match="immutable_reference"):
        TargetSpec.model_validate(raw)


def test_target_catalog_blocks_path_traversal(tmp_path: Path) -> None:
    with pytest.raises(TargetConfigError, match="invalid target id"):
        TargetCatalog(tmp_path).load("../outside")


def test_target_validate_cli_returns_a_stable_error_for_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["target-validate", str(tmp_path / "missing.yaml")]) == 2
    assert "target validation failed" in capsys.readouterr().err


def test_leased_execution_requires_resource_and_fencing_token() -> None:
    with pytest.raises(ValidationError, match="resource_id and fencing_token"):
        ExecutionRequest(
            target_id="fixture",
            argv=["python", "--version"],
            working_directory="/workspace",
            lease_scope=LeaseScope.EXCLUSIVE,
        )


def test_execution_result_rejects_false_success() -> None:
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError, match="exit_code=0"):
        ExecutionResult(
            request_id=uuid4(),
            status="succeeded",
            exit_code=1,
            started_at=now,
            finished_at=now,
            adapter_provenance=FAKE_EXECUTOR_PROVENANCE,
            synthetic=True,
        )


def test_fake_public_boundaries_are_explicitly_synthetic(tmp_path: Path) -> None:
    target = load_target(TARGET_PATH)
    request = ExecutionRequest(
        target_id=target.target_id,
        argv=["python", "--version"],
        working_directory="/workspace",
        container_image=target.inference_image.immutable_reference,
    )
    result = FakeExecutionAdapter().execute(request, target, tmp_path)
    baseline = FakeSourceManager().prepare_baseline(target, tmp_path)
    evidence = EvidenceBundle(
        task_id=uuid4(),
        target_id=target.target_id,
        evidence_type="framework_smoke",
        protocol_version="fake-v1-control-flow-only",
        adapter_provenance=[FAKE_EXECUTOR_PROVENANCE],
        synthetic=True,
    )
    artifact = ArtifactManifest(
        kind="fixture",
        uri="fake://artifact/fixture",
        content_hash="sha256:" + "0" * 64,
        synthetic=True,
    )
    assert result.synthetic is True
    assert baseline.clean is True
    assert evidence.synthetic is True
    assert artifact.synthetic is True


def test_fake_measurement_cannot_claim_samples_or_speedup() -> None:
    with pytest.raises(ValidationError, match="fake adapters cannot produce measured samples"):
        MeasurementSeries(
            status="measured",
            metric_name="latency",
            unit="ns",
            protocol_version="fake-v1-control-flow-only",
            sample_count=1,
            raw_samples_uri="fake://samples",
            raw_samples_hash="sha256:" + "0" * 64,
            environment_fingerprint="fake-environment",
            adapter_provenance=FAKE_EXECUTOR_PROVENANCE,
            synthetic=True,
        )

    with pytest.raises(ValidationError, match="synthetic results cannot contain"):
        MeasurementSeries(
            status="not_measured",
            metric_name="latency",
            unit="ns",
            protocol_version="fake-v1-control-flow-only",
            summary={"nested": {"speedup_ratio": 1.08}},
            adapter_provenance=FAKE_EXECUTOR_PROVENANCE,
            synthetic=True,
        )


def test_fake_provenance_cannot_be_published_as_real_evidence() -> None:
    with pytest.raises(ValidationError, match="must be synthetic"):
        EvidenceBundle(
            task_id=uuid4(),
            target_id="fixture",
            evidence_type="framework_smoke",
            protocol_version="fixture-v1",
            adapter_provenance=[FAKE_EXECUTOR_PROVENANCE],
            synthetic=False,
        )


def test_fake_performance_evaluation_has_no_pass_verdict() -> None:
    with pytest.raises(ValidationError, match="cannot have a pass verdict"):
        EvaluationRun(
            task_id=uuid4(),
            candidate_id=uuid4(),
            round_id=uuid4(),
            baseline_epoch_id=uuid4(),
            phase="performance",
            protocol_version="fake-v1-control-flow-only",
            target_fingerprint="sha256:" + "0" * 64,
            idempotency_key="fake-performance-run",
            passed=True,
            adapter_provenance=[FAKE_EXECUTOR_PROVENANCE],
            synthetic=True,
        )
