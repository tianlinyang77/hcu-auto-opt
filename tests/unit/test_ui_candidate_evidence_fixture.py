# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json
from pathlib import Path

from hcuopt.contracts.operator_v1 import OperatorCandidateEvidenceWorkspace


def test_ui3_demo_candidate_evidence_matches_operator_contract() -> None:
    fixture_path = (
        Path(__file__).parents[2]
        / "web"
        / "public"
        / "fixtures"
        / "demo-candidate-evidence.json"
    )
    workspace = OperatorCandidateEvidenceWorkspace.model_validate(
        json.loads(fixture_path.read_text(encoding="utf-8"))
    )

    assert [item.build.status for item in workspace.candidates] == [
        "available",
        "available",
        "failed",
    ]
    assert [item.correctness.status for item in workspace.candidates] == [
        "passed",
        "passed",
        "not_available",
    ]
    assert workspace.synthetic is True
    assert workspace.formal_signoff_allowed is False
    assert workspace.automatic_release_allowed is False
