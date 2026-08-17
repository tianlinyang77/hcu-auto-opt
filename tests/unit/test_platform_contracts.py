from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from dcuopt.adapters.fake import FakeExecutionAdapter, FakeSourceManager
from dcuopt.cli import main
from dcuopt.contracts.platform_v1 import (
    ArtifactManifest,
    EvidenceBundle,
    ExecutionRequest,
    ExecutionResult,
    TargetSpec,
)
from dcuopt.domain.enums import LeaseScope
from dcuopt.domain.errors import TargetConfigError
from dcuopt.targets import TargetCatalog, load_target

ROOT = Path(__file__).parents[2]
TARGET_PATH = ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml"


def test_locked_target_loads_as_platform_v1() -> None:
    target = load_target(TARGET_PATH)
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
