# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from hcuopt.adapters.m1_verification import M1CorrectnessEvidenceSubmission
from hcuopt.adapters.real_profile import (
    build_m1_adjudication_registry,
    build_m1_correctness_registry,
)
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.contracts.v1 import ManualPerformanceEvidenceResult
from hcuopt.evaluation.m1_verifier import (
    M1CorrectnessEvidenceV1,
    M1CorrectnessVerificationResult,
)
from hcuopt.measurement.evidence import write_evidence
from hcuopt.workers.handlers import JobHandlers
from tests.unit.test_m1_d_verifier import PROFILE, _performance, _PortableReader, _Suite


class _FixtureCorrectnessProducer:
    def __init__(self, suite: _Suite) -> None:
        raw = M1CorrectnessEvidenceV1.model_validate_json(
            _PortableReader(suite.root).read_bytes(
                suite.reference.uri,
                suite.reference.sha256,
            )
        )
        self.suite = suite
        self.provenance = raw.adapter_provenance[0]
        self.cleanup_evidence = raw.cleanup_evidence

    def produce_manual_correctness_evidence(
        self, payload, context, hotspot, output_dir
    ):
        assert context == self.suite.context
        assert hotspot == self.suite.hotspot
        assert output_dir == self.suite.root.resolve()
        return M1CorrectnessEvidenceSubmission(
            reference=self.suite.reference,
            cleanup_evidence=self.cleanup_evidence,
            adapter_provenance=(self.provenance,),
        )


def _base_payload(suite: _Suite) -> dict:
    spec = write_evidence(suite.root / "hotspot-spec.json", suite.hotspot)
    return {
        "task_id": str(suite.context.task_id),
        "candidate_id": str(suite.context.candidate_id),
        "adapter_profile": PROFILE,
        "round_id": str(uuid4()),
        "baseline_epoch_id": str(suite.context.baseline_epoch_id),
        "baseline_source": suite.baseline_snapshot.model_dump(mode="json"),
        "candidate_source": suite.candidate_snapshot.model_dump(mode="json"),
        "artifact": suite.artifact.model_dump(mode="json"),
        "target_snapshot_id": str(suite.context.target_snapshot_id),
        "target_fingerprint": suite.context.target_fingerprint,
        "target": suite.target.model_dump(mode="json"),
        "stage0_run_id": str(suite.context.stage0_run_id),
        "stage0_protocol_hash": suite.context.stage0_protocol_hash,
        "stage0_report": {
            "uri": suite.stage0_mde.uri,
            "sha256": suite.stage0_mde.sha256,
            "input_digest": suite.stage0_input_digest,
            "protocol_version": suite.stage0_protocol.protocol.protocol_version,
            "protocol_hash": suite.stage0_protocol.protocol_hash,
            "stage0_task_id": str(suite.stage0_task_id),
            "stage0_workload_id": suite.stage0_workload_id,
            "stage0_adapter_profile": suite.stage0_adapter_profile,
        },
        "workload_id": suite.context.workload_id,
        "workload_hash": suite.context.workload_hash,
        "configuration_hash": "sha256:" + "2" * 64,
        "hotspot_id": suite.hotspot.hotspot_id,
        "hotspot": {
            "correctness_spec_uri": spec.uri,
            "correctness_spec_hash": spec.sha256,
        },
        "_job_context": {
            "job_id": str(uuid4()),
            "lease_id": str(suite.context.lease_id),
            "lease_scope": "shared",
            "resource_id": suite.context.resource_id,
            "fencing_token": suite.context.fencing_token,
        },
    }


def test_m1_worker_adapters_replay_raw_correctness_and_performance(tmp_path: Path) -> None:
    suite = _Suite(tmp_path / "evidence")
    payload = _base_payload(suite)
    reader = _PortableReader(suite.root)
    correctness_registry = build_m1_correctness_registry(
        profile=PROFILE,
        protocol=suite.protocol,
        reader=reader,
        producer=_FixtureCorrectnessProducer(suite),
        evidence_root=suite.root,
    )
    correctness_result = JobHandlers(
        correctness_registry, suite.root
    ).handle_manual_correctness(payload)
    verification = M1CorrectnessVerificationResult.model_validate_json(
        reader.read_bytes(
            correctness_result["verification_artifact_uri"],
            correctness_result["verification_artifact_hash"],
        )
    )
    assert verification.verdict == "correct"

    payload["round_id"] = str(uuid4())
    _, performance = _performance(suite, verification, [0.1, 0.11, 0.09, 0.1])
    payload["round_id"] = str(suite.performance_context.round_id)
    payload["correctness_evidence_uri"] = correctness_result["raw_evidence_uri"]
    payload["correctness_evidence_hash"] = correctness_result["raw_evidence_hash"]
    payload["correctness_verification_artifact_uri"] = correctness_result[
        "verification_artifact_uri"
    ]
    payload["correctness_verification_artifact_hash"] = correctness_result[
        "verification_artifact_hash"
    ]
    payload["correctness_adapter_provenance"] = correctness_result[
        "adapter_provenance"
    ]
    payload["correctness_authority"] = {
        "job_id": payload["_job_context"]["job_id"],
        "lease_id": str(suite.context.lease_id),
        "lease_scope": "shared",
        "resource_id": suite.context.resource_id,
        "fencing_token": suite.context.fencing_token,
    }
    payload["performance_authority"] = {
        "job_id": str(uuid4()),
        "lease_id": str(suite.performance_context.lease_id),
        "lease_scope": "exclusive",
        "resource_id": suite.performance_context.resource_id,
        "fencing_token": suite.performance_context.fencing_token,
    }
    payload["performance_evidence"] = ManualPerformanceEvidenceResult(
        candidate_id=suite.context.candidate_id,
        measurement=suite.measurement,
        cleanup_evidence={
            "fence": {"fenced": True},
            "health": {"healthy": True},
        },
    ).model_dump(mode="json")
    payload["evidence_created_at"] = datetime(
        2026, 8, 25, tzinfo=timezone.utc
    ).isoformat()
    payload["_job_context"] = {
        "job_id": str(uuid4()),
        "lease_id": None,
        "lease_scope": "none",
        "resource_id": None,
        "fencing_token": None,
    }

    adjudication_registry = build_m1_adjudication_registry(
        profile=PROFILE,
        protocol=suite.protocol,
        reader=reader,
        evidence_root=suite.root,
    )
    result = JobHandlers(adjudication_registry, suite.root).handle_manual_adjudicate(
        payload
    )
    assert result["verdict"] == performance.verdict.value
    assert result["evaluation"]["synthetic"] is False
    assert result["evidence"]["summary"]["automatic_release_allowed"] is False
    assert correctness_result["verification_artifact_uri"] in result["evidence"][
        "raw_uris"
    ]
    assert any(
        item["capability"] == "candidate_adjudicator"
        for item in result["evidence"]["adapter_provenance"]
    )


def test_m1_correctness_worker_rejects_missing_spec_binding(tmp_path: Path) -> None:
    suite = _Suite(tmp_path / "evidence")
    payload = _base_payload(suite)
    payload["hotspot"] = {}
    registry = build_m1_correctness_registry(
        profile=PROFILE,
        protocol=suite.protocol,
        reader=_PortableReader(suite.root),
        producer=_FixtureCorrectnessProducer(suite),
        evidence_root=suite.root,
    )
    with pytest.raises(Exception, match="hashed correctness specification"):
        JobHandlers(registry, suite.root).handle_manual_correctness(payload)


def test_correctness_producer_and_d_adapter_use_one_profile(tmp_path: Path) -> None:
    suite = _Suite(tmp_path / "evidence")
    producer = _FixtureCorrectnessProducer(suite)
    producer.provenance = AdapterProvenance(
        profile="another-profile",
        capability=producer.provenance.capability,
        adapter_name=producer.provenance.adapter_name,
        adapter_version=producer.provenance.adapter_version,
        implementation_kind="real",
    )
    with pytest.raises(ValueError, match="another Adapter Profile"):
        build_m1_correctness_registry(
            profile=PROFILE,
            protocol=suite.protocol,
            reader=_PortableReader(suite.root),
            producer=producer,
            evidence_root=suite.root,
        )
