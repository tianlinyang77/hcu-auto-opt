# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from uuid import uuid4

import pytest

from hcuopt.domain.enums import ManualCandidateDecision
from hcuopt.domain.errors import Conflict
from hcuopt.storage.repository import require_bw20_agent_origin_for_approval


def _origin(candidate_id, source_hash="sha256:" + "a" * 64):
    return {
        "event_type": "manual_candidate_agent_origin_recorded",
        "details": {
            "schema_version": "bw20-agent-m1-origin-v1",
            "candidate_id": str(candidate_id),
            "candidate_source_hash": source_hash,
            "decision": "approved",
            "synthetic": False,
            "automatic_release_allowed": False,
        },
    }


def _validate(events, candidate_id, source_hash="sha256:" + "a" * 64):
    require_bw20_agent_origin_for_approval(
        adapter_profile="bw20-m1-manual-v1",
        decision=ManualCandidateDecision.APPROVED,
        events=events,
        candidate_id=candidate_id,
        candidate_source_hash=source_hash,
    )


def test_bw20_approval_requires_one_matching_agent_origin():
    candidate_id = uuid4()
    _validate([_origin(candidate_id)], candidate_id)

    with pytest.raises(Conflict, match="Agent origin"):
        _validate([], candidate_id)
    with pytest.raises(Conflict, match="Agent origin"):
        _validate([_origin(uuid4())], candidate_id)
    with pytest.raises(Conflict, match="Agent origin"):
        _validate([_origin(candidate_id, "sha256:" + "b" * 64)], candidate_id)
    with pytest.raises(Conflict, match="Agent origin"):
        _validate([_origin(candidate_id), _origin(candidate_id)], candidate_id)


def test_non_bw20_and_rejection_preserve_existing_signoff_behavior():
    require_bw20_agent_origin_for_approval(
        adapter_profile="other-m1-profile",
        decision=ManualCandidateDecision.APPROVED,
        events=[],
        candidate_id=uuid4(),
        candidate_source_hash="sha256:" + "a" * 64,
    )
    require_bw20_agent_origin_for_approval(
        adapter_profile="bw20-m1-manual-v1",
        decision=ManualCandidateDecision.REJECTED,
        events=[],
        candidate_id=uuid4(),
        candidate_source_hash="sha256:" + "a" * 64,
    )
