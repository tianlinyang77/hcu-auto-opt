from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from hcuopt.adapters.execution import ContainerExecutionAdapter
from hcuopt.adapters.resource_cleaner import ContainerResourceCleaner, cleanup_is_healthy
from hcuopt.contracts.platform_v1 import ExecutionRequest
from hcuopt.domain.enums import LeaseScope
from hcuopt.targets import load_target

RUN_TARGET_LOCK = os.environ.get("HCUOPT_RUN_TARGET_LOCK") == "1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TARGET_PATH = PROJECT_ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml"


@pytest.mark.target_lock
@pytest.mark.skipif(not RUN_TARGET_LOCK, reason="requires explicit Target Lock execution")
def test_real_f1b_locked_container_execution_and_cleanup() -> None:
    target = load_target(TARGET_PATH)
    assert socket.gethostname() == target.execution_host.name
    output_dir = Path(os.environ["HCUOPT_F1B_OUTPUT_DIR"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    profile = "nmz36-framework-smoke-v1"
    executor = ContainerExecutionAdapter(profile=profile)
    cleaner = ContainerResourceCleaner(target, profile=profile)
    request = ExecutionRequest(
        target_id=target.target_id,
        argv=["python", "--version"],
        working_directory="/",
        timeout_seconds=60,
        lease_scope=LeaseScope.EXCLUSIVE,
        resource_id="hcu-7",
        fencing_token=1,
        container_image=target.inference_image.immutable_reference,
    )

    result = executor.execute(request, target, output_dir)
    cleanup = cleaner.cleanup("hcu-7", 1)
    stdout_path = _file_uri_path(result.stdout_uri)
    assert result.status == "succeeded"
    assert result.exit_code == 0
    assert result.synthetic is False
    assert stdout_path.read_text(encoding="utf-8").strip() == "Python 3.10.12"
    assert result.metadata["image_id"] == target.inference_image.image_id
    assert result.metadata["registry_digest_verified"] is True
    assert result.metadata["cpu_affinity"] == "112-127"
    assert result.metadata["numa_node"] == 7
    assert result.metadata["device_index"] == 7
    assert cleanup_is_healthy(cleanup)

    evidence = {
        "target_id": target.target_id,
        "host": socket.gethostname(),
        "request": request.model_dump(mode="json"),
        "result": result.model_dump(mode="json"),
        "cleanup": cleanup,
        "performance_conclusion": "not_measured",
    }
    (output_dir / "evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _file_uri_path(uri: str | None) -> Path:
    assert uri is not None and uri.startswith("file://")
    return Path(uri.removeprefix("file://"))
