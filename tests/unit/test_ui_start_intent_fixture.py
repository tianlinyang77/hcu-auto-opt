# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json
from pathlib import Path

from hcuopt.contracts.operator_v1 import OperatorStartIntentView


def test_ui2_demo_start_intent_matches_operator_contract() -> None:
    fixture_path = (
        Path(__file__).parents[2]
        / "web"
        / "public"
        / "fixtures"
        / "demo-start-intent.json"
    )
    intent = OperatorStartIntentView.model_validate(
        json.loads(fixture_path.read_text(encoding="utf-8"))
    )

    assert intent.state == "finalized"
    assert all(member.state == "round_member_bound" for member in intent.candidate_members)
    assert intent.synthetic is True
    assert intent.automatic_release_allowed is False
