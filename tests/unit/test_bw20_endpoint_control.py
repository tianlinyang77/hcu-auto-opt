from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hcuopt.deployment.bw20_endpoint_api import build_application
from hcuopt.deployment.bw20_endpoint_control import (
    ARTIFACT_HASH,
    ARTIFACT_ID,
    BASELINE_EPOCH_ID,
    CANDIDATE_ID,
    CANDIDATE_SOURCE_HASH,
    EVIDENCE_BUNDLE_ID,
    TARGET_SNAPSHOT_ID,
    TASK_ID,
    build_provisional_request,
)

ROOT = Path(__file__).parents[2]


def _evidence(path: Path) -> str:
    value = {
        "evidence_id": str(EVIDENCE_BUNDLE_ID),
        "task_id": str(TASK_ID),
        "candidate_id": str(CANDIDATE_ID),
        "baseline_epoch_id": str(BASELINE_EPOCH_ID),
        "target_id": "bw20-sglang-0.5.12",
        "synthetic": False,
        "summary": {
            "task_id": str(TASK_ID),
            "candidate_id": str(CANDIDATE_ID),
            "baseline_epoch_id": str(BASELINE_EPOCH_ID),
            "target_snapshot_id": str(TARGET_SNAPSHOT_ID),
            "candidate_source_hash": CANDIDATE_SOURCE_HASH,
            "artifact_id": str(ARTIFACT_ID),
            "artifact_hash": ARTIFACT_HASH,
            "target_fingerprint": "sha256:" + "1" * 64,
            "measurement_environment_fingerprint": "sha256:" + "2" * 64,
            "automatic_release_allowed": False,
        },
    }
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(payload)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def test_build_request_rehashes_signed_evidence_and_freezes_one_abba(tmp_path) -> None:
    evidence = tmp_path / "evidence-bundle.json"
    evidence_hash = _evidence(evidence)

    request = build_provisional_request(
        ROOT, evidence, expected_evidence_hash=evidence_hash
    )

    assert request.environment_fingerprint == "sha256:" + "2" * 64
    assert request.signed_m1.evidence_bundle_hash == evidence_hash
    assert request.plan.acquisition_order == (
        "baseline",
        "candidate",
        "candidate",
        "baseline",
    )
    assert request.signed_m1.automatic_release_allowed is False


def test_build_request_rejects_evidence_hash_drift(tmp_path) -> None:
    evidence = tmp_path / "evidence-bundle.json"
    _evidence(evidence)

    with pytest.raises(ValueError, match="Hash differs"):
        build_provisional_request(
            ROOT, evidence, expected_evidence_hash="sha256:" + "0" * 64
        )


def test_endpoint_api_module_exposes_opt_in_route_without_migrating() -> None:
    application = build_application()

    assert "/v1/endpoint-validation-runs" in {
        route.path for route in application.routes
    }
