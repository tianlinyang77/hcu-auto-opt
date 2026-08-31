# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib

from hcuopt.contracts.agent_v1 import (
    ApexGenerationPlan,
    CandidateGenerationRequest,
    CandidateProposal,
    KnowledgeSnapshot,
)
from hcuopt.measurement.evidence import canonical_json_bytes


def _canonical_hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def knowledge_snapshot_hash(snapshot: KnowledgeSnapshot) -> str:
    value = snapshot.model_dump(mode="json")
    value["sources"] = sorted(
        value["sources"],
        key=lambda item: (item["knowledge_id"], item["version"], item["content_hash"]),
    )
    return _canonical_hash(value)


def apex_generation_plan_hash(plan: ApexGenerationPlan) -> str:
    value = plan.model_dump(mode="json")
    value["generators"] = sorted(
        value["generators"],
        key=lambda item: item["generator_id"],
    )
    return _canonical_hash(value)


def candidate_generation_request_hash(request: CandidateGenerationRequest) -> str:
    return _canonical_hash(request)


def candidate_proposal_hash(proposal: CandidateProposal) -> str:
    value = proposal.model_dump(mode="json")
    value["touched_paths"] = sorted(value["touched_paths"])
    return _canonical_hash(value)
