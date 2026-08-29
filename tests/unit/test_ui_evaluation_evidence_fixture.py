# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json
from pathlib import Path

from hcuopt.contracts.operator_v1 import OperatorEvaluationEvidenceWorkspace


def test_ui4_demo_evaluation_evidence_matches_operator_contract() -> None:
    fixture_path = (
        Path(__file__).parents[2]
        / "web"
        / "public"
        / "fixtures"
        / "demo-evaluation-evidence.json"
    )
    workspace = OperatorEvaluationEvidenceWorkspace.model_validate(
        json.loads(fixture_path.read_text(encoding="utf-8"))
    )

    assert workspace.search_status == "available"
    assert workspace.holdout_status == "available"
    assert workspace.fwer_status == "available"
    assert workspace.evidence_status == "available"
    assert workspace.search is not None
    assert len(workspace.search.members) == 3
    assert workspace.fwer is not None
    assert workspace.fwer.candidates[0].verdict == "faster"
    assert workspace.evidence_bundle is not None
    assert workspace.evidence_bundle.summary["performance_conclusion"] == "not_measured"
    assert workspace.real_performance_claim_allowed is False
    assert workspace.formal_signoff_allowed is False
    assert workspace.automatic_release_allowed is False
