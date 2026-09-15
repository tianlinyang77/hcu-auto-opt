# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from hcuopt.adapters.profiles import BW20_MANUAL_CANDIDATE_PROFILE
from hcuopt.contracts.platform_v1 import AdapterProvenance, ArtifactManifest
from hcuopt.deployment import bw20_m1_factory as factory
from hcuopt.deployment.bw20_m1_policy import BW20_M1_POLICY
from hcuopt.deployment.bw20_stage0_staging import ControllerBundle
from hcuopt.domain.errors import ExecutionSafetyError
from hcuopt.targets import load_target, target_fingerprint

ROOT = Path(__file__).resolve().parents[2]
HASH = "sha256:" + "a" * 64


def _target():
    return load_target(ROOT / "config/targets/bw20-sglang-0.5.12.yaml")


def _bundle(tmp_path):
    archive = tmp_path / "controller.tar"
    archive.write_bytes(b"fixture")
    return ControllerBundle(archive, HASH, HASH, 1)


def _provenance():
    return AdapterProvenance(
        profile=BW20_MANUAL_CANDIDATE_PROFILE,
        capability="measurement_harness",
        adapter_name="M1TrustedMeasurementHarness",
        adapter_version="1",
        implementation_kind="real",
    )


def _payload(artifact):
    return {
        "target": _target().model_dump(mode="json"),
        "target_fingerprint": target_fingerprint(_target()),
        "adapter_profile": BW20_MANUAL_CANDIDATE_PROFILE,
        "artifact": artifact.model_dump(mode="json"),
        "_job_context": {
            "job_id": str(uuid4()),
            "lease_id": str(uuid4()),
            "lease_scope": "exclusive",
            "resource_id": BW20_M1_POLICY.resource_id,
            "fencing_token": 9,
            "assert_live_lease": lambda: None,
        },
    }


def test_factory_rejects_json_that_has_no_trusted_live_lease_callback(tmp_path) -> None:
    value = factory.BW20M1WorkloadFactory(
        target=_target(),
        runner=SimpleNamespace(),
        bundle=_bundle(tmp_path),
        local_evidence_root=tmp_path / "evidence",
        harness_provenance=_provenance(),
        baseline_module_hash=HASH,
    )
    artifact = ArtifactManifest(
        candidate_id=uuid4(),
        kind="python_overlay",
        uri=(tmp_path / "candidate.py").as_uri(),
        content_hash=HASH,
        source_snapshot_id=uuid4(),
    )
    payload = _payload(artifact)
    payload["_job_context"].pop("assert_live_lease")
    with pytest.raises(ExecutionSafetyError, match="live-lease"):
        value("baseline", 0, payload, tmp_path)


def test_factory_wires_staging_session_and_mirror_without_reimplementing_harness(
    tmp_path, monkeypatch
) -> None:
    artifact_file = tmp_path / "candidate.py"
    artifact_file.write_text("# candidate\n")
    artifact = ArtifactManifest(
        candidate_id=uuid4(),
        kind="python_overlay",
        uri=artifact_file.as_uri(),
        content_hash=HASH,
        source_snapshot_id=uuid4(),
    )
    value = factory.BW20M1WorkloadFactory(
        target=_target(),
        runner=SimpleNamespace(),
        bundle=_bundle(tmp_path),
        local_evidence_root=tmp_path / "evidence",
        harness_provenance=_provenance(),
        baseline_module_hash=HASH,
    )
    def guard(plan):
        del plan

    staged = []
    monkeypatch.setattr(
        factory,
        "stage_m1_run",
        lambda **kwargs: staged.append(kwargs) or SimpleNamespace(source_guard=guard),
    )
    monkeypatch.setattr(factory, "BW20M1DockerTransport", lambda **kwargs: object())

    class Session:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            self.cleanup_complete = False
            self.identity_observations = []

        def open(self):
            self.process_id = 2
            self.process_start_token = "linux-proc-startticks:1"
            return self

    monkeypatch.setattr(factory, "BW20M1ProcessSession", Session)
    monkeypatch.setattr(factory, "BW20M1EvidenceMirror", lambda **kwargs: kwargs)
    sentinel = object()
    monkeypatch.setattr(factory, "BW20M1PairedWorkload", lambda **kwargs: sentinel)

    result = value("candidate", 1, _payload(artifact), tmp_path)

    assert result is sentinel
    assert staged[0]["artifact"] == artifact_file
    assert staged[0]["plan"].artifact_hash == HASH
    assert len(value.sessions) == 1


def test_device_timer_factory_requires_current_exclusive_job_context(tmp_path) -> None:
    value = factory.BW20M1DeviceTimerFactory(
        target=_target(), runner=SimpleNamespace(), bundle=_bundle(tmp_path)
    )
    payload = {
        "_job_context": {
            "job_id": str(uuid4()),
            "lease_id": str(uuid4()),
            "lease_scope": "shared",
            "resource_id": BW20_M1_POLICY.resource_id,
            "fencing_token": 9,
            "assert_live_lease": lambda: None,
        }
    }
    with pytest.raises(ExecutionSafetyError, match="cross-target"):
        value(payload, tmp_path)
