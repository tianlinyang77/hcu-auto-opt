from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hcuopt.adapters.profiles import (
    BW20_ENDPOINT_VALIDATION_PROFILE,
    AdapterProfileCatalog,
    bw20_endpoint_validation_profile,
)
from hcuopt.api.app import create_app
from hcuopt.contracts.endpoint_control_v1 import (
    EndpointAcquisitionResultRef,
    EndpointValidationJobResult,
    EndpointValidationRunCreate,
    endpoint_create_plan_hash,
)
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.evaluation.endpoint_workload import load_endpoint_workload_spec
from hcuopt.measurement.endpoint_models import (
    EndpointMeasurementPlan,
    SignedM1EvidenceReference,
)

ROOT = Path(__file__).parents[2]


def _uuid(value: int) -> UUID:
    return UUID(int=value)


def _hash(value: int) -> str:
    return f"sha256:{value:064x}"


def _request() -> EndpointValidationRunCreate:
    workload = load_endpoint_workload_spec(
        ROOT / "config/workloads/bw20-sglang-endpoint-provisional-v1.yaml"
    )
    return EndpointValidationRunCreate(
        name="BW20 provisional endpoint ABBA",
        signed_m1=SignedM1EvidenceReference(
            task_id=_uuid(1),
            candidate_id=_uuid(2),
            baseline_epoch_id=_uuid(3),
            target_snapshot_id=_uuid(4),
            target_id=workload.target_id,
            target_fingerprint=_hash(1),
            candidate_source_hash=_hash(2),
            artifact_id=_uuid(5),
            artifact_hash=_hash(3),
            evidence_bundle_id=_uuid(6),
            evidence_bundle_hash=_hash(4),
            signoff_id=_uuid(7),
        ),
        workload=workload,
        plan=EndpointMeasurementPlan(
            run_mode="provisional",
            acquisition_order=("baseline", "candidate", "candidate", "baseline"),
            warmup_requests=1,
            measured_requests_per_acquisition=2,
            ready_timeout_seconds=300,
            request_timeout_seconds=60,
        ),
        environment_fingerprint=_hash(5),
        adapter_profile=BW20_ENDPOINT_VALIDATION_PROFILE,
        idempotency_key="bw20-endpoint-provisional-fixture-v1",
    )


def test_endpoint_create_contract_is_one_non_releasing_provisional_abba() -> None:
    request = _request()

    assert request.plan.run_mode == "provisional"
    assert endpoint_create_plan_hash(request).startswith("sha256:")
    with pytest.raises(ValidationError, match="first endpoint.*provisional"):
        EndpointValidationRunCreate.model_validate(
            {
                **request.model_dump(mode="python"),
                "plan": request.plan.model_copy(update={"run_mode": "formal"}),
            }
        )


def test_endpoint_job_result_requires_real_complete_abba_and_healthy_cleanup() -> None:
    request = _request()
    result = EndpointValidationJobResult(
        endpoint_run_id=_uuid(8),
        plan_hash=endpoint_create_plan_hash(request),
        staging_receipt_hash=_hash(20),
        acquisitions=tuple(
            EndpointAcquisitionResultRef(
                acquisition_ordinal=ordinal,
                arm=arm,
                evidence_uri=f"file:///endpoint/{ordinal}",
                result_sha256=_hash(30 + ordinal),
                activation_sha256=_hash(40 + ordinal),
                cache_namespace_sha256=_hash(50 + ordinal),
                cleanup_succeeded=True,
            )
            for ordinal, arm in enumerate(request.plan.acquisition_order)
        ),
        cleanup_evidence={
            "fence": {"fenced": True},
            "health": {"healthy": True},
        },
        adapter_provenance=(
            AdapterProvenance(
                profile=BW20_ENDPOINT_VALIDATION_PROFILE,
                capability="endpoint_measurement_runner",
                adapter_name="BW20EndpointRunner",
                adapter_version="1",
                implementation_kind="real",
            ),
        ),
    )

    assert result.producer_verdict is None
    assert result.automatic_release_allowed is False
    with pytest.raises(ValidationError, match="fenced cleanup"):
        EndpointValidationJobResult.model_validate(
            {
                **result.model_dump(mode="python"),
                "cleanup_evidence": {
                    "fence": {"fenced": False},
                    "health": {"healthy": True},
                },
            }
        )


def test_endpoint_profile_is_explicit_opt_in_and_api_keeps_frozen_workload() -> None:
    request = _request()
    run_id = _uuid(8)
    task_id = _uuid(9)
    job_id = _uuid(10)

    class Repository:
        captured = None

        def migrate(self) -> None:
            pass

        def create_endpoint_validation_run(self, payload):
            self.captured = payload
            now = datetime.now(timezone.utc)
            return {
                "endpoint_run_id": run_id,
                "task_id": task_id,
                "job_id": job_id,
                "signed_m1_task_id": payload.signed_m1.task_id,
                "target_snapshot_id": payload.signed_m1.target_snapshot_id,
                "adapter_profile": payload.adapter_profile,
                "environment_fingerprint": payload.environment_fingerprint,
                "workload": payload.workload.model_dump(mode="json"),
                "plan": payload.plan.model_dump(mode="json"),
                "plan_hash": endpoint_create_plan_hash(payload),
                "state": "queued",
                "result": None,
                "automatic_release_allowed": False,
                "created_at": now,
                "updated_at": now,
            }

    repository = Repository()
    with pytest.raises(Exception, match="not registered"):
        AdapterProfileCatalog().require(BW20_ENDPOINT_VALIDATION_PROFILE)
    app = create_app(
        repository=repository,
        adapter_profiles=AdapterProfileCatalog((bw20_endpoint_validation_profile(),)),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/endpoint-validation-runs", json=request.model_dump(mode="json")
        )

    assert response.status_code == 201, response.text
    assert response.json()["endpoint_run_id"] == str(run_id)
    assert repository.captured == request
