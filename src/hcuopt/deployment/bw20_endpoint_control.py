# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Create the one frozen BW20 provisional Endpoint Validation Run."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

from hcuopt.adapters.profiles import BW20_ENDPOINT_VALIDATION_PROFILE
from hcuopt.contracts.endpoint_control_v1 import EndpointValidationRunCreate
from hcuopt.evaluation.endpoint_workload import load_endpoint_workload_spec
from hcuopt.measurement.endpoint_models import (
    EndpointMeasurementPlan,
    SignedM1EvidenceReference,
)

TASK_ID = UUID("73f6f07d-14ed-5614-a8e5-75e77c4356f0")
CANDIDATE_ID = UUID("bbcdc4f0-0369-54d0-b4cd-57763203c272")
BASELINE_EPOCH_ID = UUID("844a19f1-caa4-54fd-9232-32b036e2f60f")
TARGET_SNAPSHOT_ID = UUID("a8c8a876-5e0a-5dee-b759-de1b05054c63")
CANDIDATE_SOURCE_HASH = (
    "sha256:f27c1546bc5bd46741ae98ad0b96d51974a0108af316f9d557daa2f82556fbc2"
)
ARTIFACT_ID = UUID("4f81e3af-5a06-5684-ade0-51fc65bba9d2")
ARTIFACT_HASH = (
    "sha256:93bd2a5aec5a01cb61ac8c6f2ddca7bd25d63ffe4aec8fed7f60199c3581da8a"
)
EVIDENCE_BUNDLE_ID = UUID("d1c3ca75-46de-5aab-92d3-4a0203f95f8b")
EVIDENCE_BUNDLE_HASH = (
    "sha256:efb0177b072557744db4cb169b3a5550094f1760bb14a347702020c56a7e0a0d"
)
SIGNOFF_ID = UUID("e131588e-546c-5553-9095-0ce8fc5d38ea")
WORKLOAD_PATH = Path("config/workloads/bw20-sglang-endpoint-provisional-v1.yaml")
IDEMPOTENCY_KEY = "bw20-endpoint-provisional-20260916-v3"


def _read_hashed_json(path: Path, expected_hash: str) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    before = path.lstat()
    if (
        path.is_symlink()
        or resolved != path.absolute()
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not 0 < before.st_size <= 16 * 1024 * 1024
    ):
        raise ValueError("unsafe signed M1 EvidenceBundle file")
    payload = path.read_bytes()
    after = path.lstat()
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ):
        raise ValueError("signed M1 EvidenceBundle changed while reading")
    actual = "sha256:" + hashlib.sha256(payload).hexdigest()
    if actual != expected_hash:
        raise ValueError("signed M1 EvidenceBundle Hash differs")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("signed M1 EvidenceBundle is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("signed M1 EvidenceBundle is not an object")
    return value


def build_provisional_request(
    source_root: Path,
    evidence_bundle: Path,
    *,
    expected_evidence_hash: str = EVIDENCE_BUNDLE_HASH,
) -> EndpointValidationRunCreate:
    root = source_root.resolve(strict=True)
    if source_root.is_symlink() or root != source_root.absolute():
        raise ValueError("endpoint source root must not be redirected")
    raw = _read_hashed_json(evidence_bundle, expected_evidence_hash)
    summary = raw.get("summary")
    expected = {
        "task_id": str(TASK_ID),
        "candidate_id": str(CANDIDATE_ID),
        "baseline_epoch_id": str(BASELINE_EPOCH_ID),
        "target_snapshot_id": str(TARGET_SNAPSHOT_ID),
        "candidate_source_hash": CANDIDATE_SOURCE_HASH,
        "artifact_id": str(ARTIFACT_ID),
        "artifact_hash": ARTIFACT_HASH,
        "automatic_release_allowed": False,
    }
    if (
        raw.get("evidence_id") != str(EVIDENCE_BUNDLE_ID)
        or raw.get("task_id") != str(TASK_ID)
        or raw.get("candidate_id") != str(CANDIDATE_ID)
        or raw.get("baseline_epoch_id") != str(BASELINE_EPOCH_ID)
        or raw.get("target_id") != "bw20-sglang-0.5.12"
        or raw.get("synthetic") is not False
        or not isinstance(summary, dict)
        or any(summary.get(name) != value for name, value in expected.items())
    ):
        raise ValueError("signed M1 EvidenceBundle identity differs")
    target_fingerprint = summary.get("target_fingerprint")
    environment_fingerprint = summary.get("measurement_environment_fingerprint")
    if not isinstance(target_fingerprint, str) or not isinstance(
        environment_fingerprint, str
    ):
        raise ValueError("signed M1 EvidenceBundle lacks environment identity")
    workload = load_endpoint_workload_spec(root / WORKLOAD_PATH)
    return EndpointValidationRunCreate(
        name="BW20 SGLang endpoint provisional ABBA",
        signed_m1=SignedM1EvidenceReference(
            task_id=TASK_ID,
            candidate_id=CANDIDATE_ID,
            baseline_epoch_id=BASELINE_EPOCH_ID,
            target_snapshot_id=TARGET_SNAPSHOT_ID,
            target_id=workload.target_id,
            target_fingerprint=target_fingerprint,
            candidate_source_hash=CANDIDATE_SOURCE_HASH,
            artifact_id=ARTIFACT_ID,
            artifact_hash=ARTIFACT_HASH,
            evidence_bundle_id=EVIDENCE_BUNDLE_ID,
            evidence_bundle_hash=expected_evidence_hash,
            signoff_id=SIGNOFF_ID,
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
        environment_fingerprint=environment_fingerprint,
        adapter_profile=BW20_ENDPOINT_VALIDATION_PROFILE,
        idempotency_key=IDEMPOTENCY_KEY,
    )


def _endpoint_url(api_url: str) -> str:
    parsed = urlsplit(api_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("endpoint control command requires an explicit loopback API port")
    return api_url.rstrip("/") + "/v1/endpoint-validation-runs"


def create_run(api_url: str, request: EndpointValidationRunCreate) -> dict[str, Any]:
    wire = request.model_dump_json().encode("utf-8")
    http_request = Request(
        _endpoint_url(api_url),
        data=wire,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(http_request, timeout=30) as response:  # noqa: S310 - loopback enforced
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("endpoint API response is not an object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:18003")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--evidence-bundle", type=Path, required=True)
    args = parser.parse_args(argv)
    request = build_provisional_request(args.source_root, args.evidence_bundle)
    result = create_run(args.api_url, request)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_provisional_request", "create_run", "main"]
