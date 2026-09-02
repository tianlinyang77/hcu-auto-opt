# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json
from pathlib import Path

from hcuopt.contracts.agent_verification_v1 import AgentGenerationReadModel


def test_ui5_demo_agent_proposals_match_d_read_model() -> None:
    fixture_path = (
        Path(__file__).parents[2]
        / "web"
        / "public"
        / "fixtures"
        / "demo-agent-proposals.json"
    )
    workspace = AgentGenerationReadModel.model_validate(
        json.loads(fixture_path.read_text(encoding="utf-8"))
    )

    assert [item.status for item in workspace.attempts] == [
        "timed_out",
        "succeeded",
        "succeeded",
    ]
    assert [item.status for item in workspace.proposals] == ["kept", "eliminated"]
    assert workspace.proposals[0].lifecycle.promotion is not None
    assert workspace.proposals[0].lifecycle.promotion_status == "promoted"
    assert workspace.proposals[1].reason_code == "duplicate_normalized_patch"
    assert workspace.formal_readiness == "hold"
    assert workspace.performance_conclusion == "not_measured"
    assert workspace.formal_intake_allowed is False
    assert workspace.automatic_release_allowed is False
