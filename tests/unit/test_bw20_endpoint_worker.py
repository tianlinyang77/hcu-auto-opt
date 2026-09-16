from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from hcuopt.adapters.bw20_endpoint_execution import PROFILE
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.deployment.bw20_endpoint_worker import BW20EndpointMeasurementRunner
from hcuopt.domain.errors import ExecutionSafetyError
from hcuopt.targets import load_target
from hcuopt.workers.handlers import JobHandlers

ROOT = Path(__file__).parents[2]
TARGET = load_target(ROOT / "config/targets/bw20-sglang-0.5.12.yaml")


class EndpointRunnerFixture:
    provenance = AdapterProvenance(
        profile=PROFILE,
        capability="endpoint_measurement_runner",
        adapter_name="EndpointRunnerFixture",
        adapter_version="1",
        implementation_kind="real",
    )

    def run_endpoint_validation(self, payload, output_dir):
        assert payload["endpoint_run_id"]
        assert output_dir.is_dir()
        return {
            "status": "provisional_passed",
            "adapter_provenance": [self.provenance.model_dump(mode="json")],
            "producer_verdict": None,
            "automatic_release_allowed": False,
        }


def test_job_handler_uses_explicit_endpoint_runner_boundary(tmp_path: Path) -> None:
    handlers = JobHandlers(
        AdapterRegistry(
            profile=PROFILE,
            endpoint_measurement_runner=EndpointRunnerFixture(),
        ),
        output_dir=tmp_path,
    )

    result = handlers.handle_endpoint_validation({"endpoint_run_id": str(uuid4())})

    assert result["status"] == "provisional_passed"
    assert result["producer_verdict"] is None
    assert result["automatic_release_allowed"] is False


def test_bw20_runner_rejects_payload_without_trusted_exclusive_lease(
    tmp_path: Path,
) -> None:
    runner = BW20EndpointMeasurementRunner(
        target=TARGET,
        source_root=ROOT,
        runner=object(),  # type: ignore[arg-type]
        stage=lambda **kwargs: pytest.fail("staging must not start"),
        executor_factory=lambda root: pytest.fail("executor must not start"),
    )

    with pytest.raises(ExecutionSafetyError, match="trusted Job context"):
        runner.run_endpoint_validation({}, tmp_path)
