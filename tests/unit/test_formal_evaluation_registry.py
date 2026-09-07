# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from pathlib import Path

import pytest

from hcuopt.domain.errors import SourceArtifactError
from hcuopt.evaluation.m2_formal_registration import _decode, evaluation_registration_hash
from tests.unit.test_formal_evaluation_start_authority import _registration
from tests.unit.test_formal_operator_plans import _fixture, _hash


def test_registration_reader_rehashes_content_and_checks_every_index(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    registration = _registration(fixture, preview, tmp_path)
    row = {
        "registration_id": registration.registration_id,
        "preview_id": registration.preview_id,
        "formal_authorization_hash": registration.formal_authorization_hash,
        "resolved_plan_hash": registration.resolved_plan_hash,
        "content_hash": evaluation_registration_hash(registration),
        "registration": registration.model_dump(mode="json"),
    }
    assert _decode(row) == registration
    for field in row.keys() - {"registration"}:
        with pytest.raises(SourceArtifactError):
            _decode(row | {field: "invalid"})
    with pytest.raises(SourceArtifactError):
        _decode(row | {"registration": row["registration"] | {
            "holdout_plan_commitment": _hash("tampered")
        }})
