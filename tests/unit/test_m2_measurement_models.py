# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from hcuopt.domain.enums import RoundPhase
from hcuopt.measurement.m2_models import (
    M2ScriptedPhaseReceipt,
    RoundMeasurementRef,
    validate_round_measurement_refs,
)


def _hash(digit: str) -> str:
    return "sha256:" + digit * 64


def _uuid(value: int) -> UUID:
    return UUID(int=value)


def _formal_reference(
    ordinal: int,
    *,
    phase: RoundPhase = RoundPhase.SEARCH,
    candidate_family_hash: str | None = None,
    artifact_family_hash: str | None = None,
    **updates: object,
) -> RoundMeasurementRef:
    holdout = phase is RoundPhase.HOLDOUT
    values: dict[str, object] = {
        "round_measurement_ref_id": _uuid(100 + ordinal),
        "round_id": _uuid(1),
        "round_candidate_id": _uuid(200 + ordinal),
        "candidate_id": _uuid(300 + ordinal),
        "phase": phase,
        "candidate_family_hash": candidate_family_hash or _hash("a"),
        "artifact_family_hash": artifact_family_hash or _hash("b"),
        "holdout_family_hash": _hash("c") if holdout else None,
        "artifact_id": _uuid(400 + ordinal),
        "artifact_hash": _hash(str((ordinal + 1) % 10)),
        "measurement_id": _uuid(500 + ordinal),
        "raw_evidence_uri": f"file:///evidence/{ordinal}.json",
        "raw_evidence_hash": _hash(str((ordinal + 2) % 10)),
        "measurement_plan_hash": _hash(str((ordinal + 3) % 10)),
        "phase_plan_hash": _hash(str((ordinal + 4) % 10)),
        "holdout_reveal_evidence_hash": _hash("d") if holdout else None,
        "baseline_sample_set_hash": _hash(str((ordinal + 5) % 10)),
        "process_identity_set_hash": _hash(str((ordinal + 6) % 10)),
        "cache_namespace_set_hash": _hash(str((ordinal + 7) % 10)),
        "lease_id": _uuid(600 + ordinal),
        "resource_id": f"scripted-resource-{ordinal}",
        "fencing_token": ordinal + 1,
        "created_at": datetime(2026, 8, 27, tzinfo=timezone.utc),
    }
    values.update(updates)
    return RoundMeasurementRef.model_validate(values)


def test_formal_reference_enforces_search_holdout_discriminator() -> None:
    search = _formal_reference(0)
    holdout = _formal_reference(1, phase=RoundPhase.HOLDOUT)

    assert search.holdout_family_hash is None
    assert search.holdout_reveal_evidence_hash is None
    assert holdout.holdout_family_hash == _hash("c")
    assert holdout.synthetic is False
    assert holdout.status == "measured"

    with pytest.raises(ValidationError, match="cannot contain Holdout"):
        _formal_reference(2, holdout_family_hash=_hash("c"))
    with pytest.raises(ValidationError, match="requires Family and reveal"):
        _formal_reference(
            3,
            phase=RoundPhase.HOLDOUT,
            holdout_reveal_evidence_hash=None,
        )


def test_formal_reference_rejects_synthetic_scripted_verdict_and_naive_time() -> None:
    with pytest.raises(ValidationError, match="Input should be False"):
        _formal_reference(0, synthetic=True)
    with pytest.raises(ValidationError, match="m1-kernel-performance-evidence-v1"):
        _formal_reference(
            0,
            evidence_schema_version="m2a-scripted-phase-evidence-v1",
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _formal_reference(0, producer_verdict="faster")
    with pytest.raises(ValidationError, match="timezone-aware"):
        _formal_reference(0, created_at=datetime(2026, 8, 27))


@pytest.mark.parametrize(
    "field_name",
    [
        "round_measurement_ref_id",
        "measurement_id",
        "raw_evidence_uri",
        "raw_evidence_hash",
        "baseline_sample_set_hash",
        "process_identity_set_hash",
        "cache_namespace_set_hash",
    ],
)
def test_formal_authority_rejects_identity_reuse(field_name: str) -> None:
    first = _formal_reference(0)
    second = _formal_reference(
        1,
        phase=RoundPhase.HOLDOUT,
        **{field_name: getattr(first, field_name)},
    )

    with pytest.raises(ValueError, match=f"reuses {field_name}"):
        validate_round_measurement_refs((first, second))


def test_formal_authority_rejects_member_phase_and_family_drift() -> None:
    first = _formal_reference(0)
    duplicate_member_phase = _formal_reference(1, candidate_id=first.candidate_id)
    family_drift = _formal_reference(2, candidate_family_hash=_hash("e"))

    with pytest.raises(ValueError, match="Candidate Phase already"):
        validate_round_measurement_refs((first, duplicate_member_phase))
    with pytest.raises(ValueError, match="different frozen families"):
        validate_round_measurement_refs((first, family_drift))


def test_scripted_receipt_cannot_be_revalidated_as_formal_reference() -> None:
    values = _formal_reference(0).model_dump(mode="python")
    values.pop("round_measurement_ref_id")
    values["scripted_phase_receipt_id"] = _uuid(700)
    values["schema_version"] = "m2a-scripted-phase-receipt-v1"
    values["evidence_schema_version"] = "m2a-scripted-phase-evidence-v1"
    values["status"] = "not_measured"
    values["synthetic"] = True
    receipt = M2ScriptedPhaseReceipt.model_validate(values)

    assert receipt.status == "not_measured"
    assert receipt.synthetic is True
    with pytest.raises(ValidationError):
        RoundMeasurementRef.model_validate(receipt.model_dump(mode="python"))
    with pytest.raises(TypeError, match="only RoundMeasurementRef"):
        validate_round_measurement_refs((receipt,))  # type: ignore[arg-type]
