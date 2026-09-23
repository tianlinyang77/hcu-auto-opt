# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from hcuopt.contracts.endpoint_source_v1 import (
    ENDPOINT_SOURCE_ADAPTER,
    FormalEndpointSource,
    M1EndpointSource,
)
from hcuopt.measurement.endpoint_models import SignedM1EvidenceReference


def formal_wire():
    payload = {name: str(uuid4()) for name in (
        "task_id", "round_id", "candidate_id", "baseline_epoch_id", "target_snapshot_id",
        "artifact_id", "round_evidence_bundle_id", "round_signoff_id",
    )}
    payload.update({name: "sha256:" + "a" * 64 for name in (
        "candidate_source_hash", "artifact_hash", "evidence_bundle_hash", "decision_artifact_hash",
    )})
    return {"kind": "signed_formal_round", **payload}


def test_formal_reference_roundtrips_without_claiming_admission():
    reference = ENDPOINT_SOURCE_ADAPTER.validate_json(json.dumps(formal_wire()))
    assert isinstance(reference, FormalEndpointSource)
    assert ENDPOINT_SOURCE_ADAPTER.validate_json(reference.model_dump_json()) == reference
    assert reference.automatic_release_allowed is False
    assert "accepted" not in reference.model_dump()


@pytest.mark.parametrize("fault", ["missing_round", "wrong_kind", "bad_hash", "release", "extra"])
def test_formal_rejects_incomplete_or_mixed_references(fault):
    value = formal_wire()
    if fault == "missing_round":
        del value["round_id"]
    elif fault == "wrong_kind":
        value["kind"] = "signed_m1"
    elif fault == "bad_hash":
        value["artifact_hash"] = "latest"
    elif fault == "release":
        value["automatic_release_allowed"] = True
    else:
        value["accepted"] = True
    with pytest.raises(ValidationError):
        ENDPOINT_SOURCE_ADAPTER.validate_json(json.dumps(value))


def test_m1_reference_is_preserved_and_formal_cannot_impersonate_it():
    wire = formal_wire()
    evidence = {name: wire[name] for name in (
        "task_id", "candidate_id", "baseline_epoch_id", "target_snapshot_id",
        "candidate_source_hash", "artifact_id", "artifact_hash", "evidence_bundle_hash",
    )}
    evidence.update(target_id="bw20", target_fingerprint="sha256:" + "b" * 64,
                    evidence_bundle_id=str(uuid4()), signoff_id=str(uuid4()))
    original = SignedM1EvidenceReference.model_validate_json(json.dumps(evidence))
    wrapped = ENDPOINT_SOURCE_ADAPTER.validate_json(json.dumps({
        "kind": "signed_m1", "evidence": evidence,
    }))
    assert isinstance(wrapped, M1EndpointSource)
    assert wrapped.evidence == original
    with pytest.raises(ValidationError):
        SignedM1EvidenceReference.model_validate_json(json.dumps(wire))
