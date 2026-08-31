# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from hcuopt.adapters.interfaces import CandidateGeneratorAdapter
from hcuopt.agent.identity import (
    apex_generation_plan_hash,
    candidate_generation_request_hash,
    candidate_proposal_hash,
    knowledge_snapshot_hash,
)
from hcuopt.contracts.agent_v1 import (
    ApexGenerationPlan,
    CandidateGenerationRequest,
    CandidateProposal,
    CandidateProposalBatch,
    KnowledgeSnapshot,
)
from hcuopt.contracts.platform_v1 import AdapterProvenance

FIXED_TIME = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _knowledge() -> KnowledgeSnapshot:
    return KnowledgeSnapshot(
        snapshot_id=UUID("00000000-0000-0000-0000-000000000001"),
        sources=(
            {
                "knowledge_id": "operator/trace-evidence",
                "source_kind": "operator_evidence",
                "version": "v1",
                "source_uri": "evidence:///operator/trace.json",
                "content_hash": _hash("1"),
                "license_id": "internal-evidence",
            },
            {
                "knowledge_id": "skills/triton-guidance",
                "source_kind": "skill",
                "version": "1.0.0",
                "source_uri": "skill://triton-guidance/1.0.0",
                "content_hash": _hash("2"),
                "license_id": "MIT",
            },
        ),
        created_by="candidate-authority",
        created_at=FIXED_TIME,
    )


def _request() -> CandidateGenerationRequest:
    knowledge = _knowledge()
    return CandidateGenerationRequest(
        request_id=UUID("00000000-0000-0000-0000-000000000002"),
        generation_run_id=UUID("00000000-0000-0000-0000-000000000003"),
        target_snapshot_id=UUID("00000000-0000-0000-0000-000000000004"),
        stage0_run_id=UUID("00000000-0000-0000-0000-000000000005"),
        baseline_epoch_id=UUID("00000000-0000-0000-0000-000000000006"),
        baseline_source_hash=_hash("3"),
        hotspot_id=UUID("00000000-0000-0000-0000-000000000007"),
        replacement_point="sglang.runtime.operator.forward",
        workload_id="agent-contract-fixture",
        workload_hash=_hash("4"),
        configuration_hash=_hash("5"),
        image_digest=_hash("6"),
        profiler_evidence_uri="evidence:///operator/profile.json",
        profiler_evidence_hash=_hash("7"),
        knowledge_snapshot_id=knowledge.snapshot_id,
        knowledge_snapshot_hash=knowledge_snapshot_hash(knowledge),
        max_proposals=2,
    )


def _proposal(ordinal: int = 0) -> CandidateProposal:
    request = _request()
    return CandidateProposal(
        proposal_id=UUID(f"00000000-0000-0000-0000-{ordinal + 10:012d}"),
        request_id=request.request_id,
        generation_run_id=request.generation_run_id,
        generator_id="deterministic-agent",
        ordinal=ordinal,
        optimization_intent="reduce redundant index materialization",
        rationale="The frozen trace shows repeated index conversion on the selected path.",
        risk_summary="Requires independent correctness review for non-contiguous inputs.",
        patch_uri=f"proposal:///deterministic-agent/{ordinal}.diff",
        patch_hash=_hash("8" if ordinal == 0 else "9"),
        normalized_patch_hash=_hash("a" if ordinal == 0 else "b"),
        touched_paths=("sglang/runtime/operator.py",),
        replacement_point=request.replacement_point,
    )


def _batch(*, synthetic: bool = True) -> CandidateProposalBatch:
    request = _request()
    return CandidateProposalBatch(
        batch_id=UUID("00000000-0000-0000-0000-000000000020"),
        request_id=request.request_id,
        generation_run_id=request.generation_run_id,
        generator_id="deterministic-agent",
        adapter_provenance=AdapterProvenance(
            profile="m2b-agent-contract-test",
            capability="candidate_proposal_generation",
            adapter_name="DeterministicAgent",
            adapter_version="1.0.0",
            implementation_kind="fake",
        ),
        status="succeeded",
        proposals=(_proposal(),),
        raw_output_uri="proposal:///deterministic-agent/raw.json",
        raw_output_hash=_hash("c"),
        output_bytes=1024,
        attempt_count=1,
        wall_seconds=0.1,
        started_at=FIXED_TIME,
        finished_at=FIXED_TIME,
        synthetic=synthetic,
    )


def _plan() -> ApexGenerationPlan:
    request = _request()
    return ApexGenerationPlan(
        plan_id=UUID("00000000-0000-0000-0000-000000000030"),
        generation_run_id=request.generation_run_id,
        request_id=request.request_id,
        request_hash=candidate_generation_request_hash(request),
        generators=(
            {
                "generator_id": "agent-a",
                "adapter_profile": "m2b-agent-a-v1",
                "max_attempts": 2,
                "max_proposals": 2,
                "timeout_seconds": 600,
            },
            {
                "generator_id": "agent-b",
                "adapter_profile": "m2b-agent-b-v1",
                "max_attempts": 1,
                "max_proposals": 2,
                "timeout_seconds": 600,
            },
        ),
        max_concurrency=2,
        budget={
            "max_generator_attempts": 3,
            "max_wall_seconds": 1800,
            "max_total_output_bytes": 1_000_000,
            "max_total_tokens": 100_000,
            "max_proposals": 4,
        },
        created_by="apex-control-plane",
        created_at=FIXED_TIME,
    )


def test_knowledge_and_plan_hashes_ignore_declaration_order() -> None:
    knowledge = _knowledge()
    reordered_knowledge = KnowledgeSnapshot.model_validate(
        {**knowledge.model_dump(mode="json"), "sources": list(reversed(knowledge.sources))}
    )
    plan = _plan()
    reordered_plan = ApexGenerationPlan.model_validate(
        {**plan.model_dump(mode="json"), "generators": list(reversed(plan.generators))}
    )

    assert knowledge_snapshot_hash(knowledge) == knowledge_snapshot_hash(reordered_knowledge)
    assert apex_generation_plan_hash(plan) == apex_generation_plan_hash(reordered_plan)


def test_agent_contracts_reject_duplicate_or_over_budget_authority() -> None:
    knowledge = _knowledge()
    with pytest.raises(ValidationError, match="duplicate source version"):
        KnowledgeSnapshot.model_validate(
            {**knowledge.model_dump(mode="json"), "sources": [knowledge.sources[0]] * 2}
        )

    plan = _plan()
    with pytest.raises(ValidationError, match="attempts exceed"):
        ApexGenerationPlan.model_validate(
            {
                **plan.model_dump(mode="json"),
                "budget": {**plan.budget.model_dump(), "max_generator_attempts": 2},
            }
        )
    with pytest.raises(ValidationError, match="concurrency exceeds"):
        ApexGenerationPlan.model_validate(
            {**plan.model_dump(mode="json"), "max_concurrency": 3}
        )


@pytest.mark.parametrize(
    "field",
    ["holdout_access_allowed", "hcu_access_allowed", "measurement_access_allowed"],
)
def test_generation_request_cannot_enable_protected_access(field: str) -> None:
    request = _request().model_dump(mode="json")
    request[field] = True

    with pytest.raises(ValidationError):
        CandidateGenerationRequest.model_validate(request)


def test_proposal_is_review_only_and_hashes_paths_canonically() -> None:
    proposal = _proposal()
    expanded = proposal.model_copy(
        update={
            "touched_paths": (
                "sglang/runtime/operator.py",
                "sglang/runtime/helpers.py",
            )
        }
    )
    reordered = CandidateProposal.model_validate(
        {
            **expanded.model_dump(mode="json"),
            "touched_paths": list(reversed(expanded.touched_paths)),
        }
    )

    assert candidate_proposal_hash(expanded) == candidate_proposal_hash(reordered)
    assert proposal.review_required is True
    assert proposal.formal_intake_allowed is False
    assert proposal.performance_conclusion == "not_measured"
    with pytest.raises(ValidationError, match="normalized Python source"):
        CandidateProposal.model_validate(
            {**proposal.model_dump(mode="json"), "touched_paths": ["../escape.py"]}
        )


def test_batch_rejects_fake_non_synthetic_and_cross_generator_output() -> None:
    with pytest.raises(ValidationError, match="must be synthetic"):
        _batch(synthetic=False)

    batch = _batch().model_dump(mode="json")
    batch["proposals"][0]["generator_id"] = "another-agent"
    with pytest.raises(ValidationError, match="cross-generator"):
        CandidateProposalBatch.model_validate(batch)


def test_candidate_generator_adapter_is_proposal_only_runtime_protocol(tmp_path: Path) -> None:
    class DeterministicAdapter:
        provenance = _batch().adapter_provenance

        def generate_proposals(
            self,
            request: CandidateGenerationRequest,
            output_dir: Path,
        ) -> CandidateProposalBatch:
            assert request == _request()
            assert output_dir == tmp_path
            return _batch()

    adapter = DeterministicAdapter()

    assert isinstance(adapter, CandidateGeneratorAdapter)
    assert adapter.generate_proposals(_request(), tmp_path).performance_conclusion == (
        "not_measured"
    )
