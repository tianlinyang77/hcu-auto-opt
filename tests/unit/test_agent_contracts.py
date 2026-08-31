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
    candidate_proposal_batch_hash,
    candidate_proposal_hash,
    candidate_proposal_promotion_receipt_hash,
    candidate_proposal_review_record_hash,
    knowledge_snapshot_hash,
    verify_candidate_proposal_patch,
    verify_candidate_proposal_promotion_receipt,
    verify_candidate_proposal_review_record,
)
from hcuopt.agent.patch_identity import (
    PatchIdentityError,
    normalize_patch_v1,
    normalized_patch_hash_v1,
    raw_patch_hash_v1,
)
from hcuopt.contracts.agent_v1 import (
    ApexGenerationPlan,
    CandidateGenerationRequest,
    CandidateProposal,
    CandidateProposalBatch,
    CandidateProposalPromotionReceipt,
    CandidateProposalReviewRecord,
    KnowledgeSnapshot,
)
from hcuopt.contracts.m2 import CandidateSourcePackageRef
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
        max_proposals=4,
    )


def _raw_patch(ordinal: int = 0) -> bytes:
    return (
        "diff --git a/sglang/runtime/operator.py b/sglang/runtime/operator.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/sglang/runtime/operator.py\n"
        "+++ b/sglang/runtime/operator.py\n"
        "@@ -1 +1 @@\n"
        "-return value\n"
        f"+return value + {ordinal + 1}\n"
    ).encode()


def _proposal(ordinal: int = 0) -> CandidateProposal:
    request = _request()
    raw_patch = _raw_patch(ordinal)
    return CandidateProposal(
        proposal_id=UUID(f"00000000-0000-0000-0000-{ordinal + 10:012d}"),
        request_id=request.request_id,
        request_hash=candidate_generation_request_hash(request),
        generation_run_id=request.generation_run_id,
        generator_id="deterministic-agent",
        ordinal=ordinal,
        optimization_intent="reduce redundant index materialization",
        rationale="The frozen trace shows repeated index conversion on the selected path.",
        risk_summary="Requires independent correctness review for non-contiguous inputs.",
        patch_uri=f"proposal:///deterministic-agent/{ordinal}.diff",
        patch_hash=raw_patch_hash_v1(raw_patch),
        normalized_patch_hash=normalized_patch_hash_v1(raw_patch),
        touched_paths=("sglang/runtime/operator.py",),
        replacement_point=request.replacement_point,
    )


def _batch(*, synthetic: bool = True) -> CandidateProposalBatch:
    request = _request()
    return CandidateProposalBatch(
        batch_id=UUID("00000000-0000-0000-0000-000000000020"),
        request_id=request.request_id,
        request_hash=candidate_generation_request_hash(request),
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


def _review(*, decision: str = "approved") -> CandidateProposalReviewRecord:
    request = _request()
    proposal = _proposal()
    return CandidateProposalReviewRecord(
        review_id=UUID("00000000-0000-0000-0000-000000000040"),
        idempotency_key="review-proposal-000000000040",
        proposal_id=proposal.proposal_id,
        proposal_hash=candidate_proposal_hash(proposal),
        request_id=request.request_id,
        request_hash=candidate_generation_request_hash(request),
        generation_run_id=request.generation_run_id,
        patch_uri=proposal.patch_uri,
        patch_hash=proposal.patch_hash,
        normalized_patch_hash=proposal.normalized_patch_hash,
        baseline_epoch_id=request.baseline_epoch_id,
        baseline_source_hash=request.baseline_source_hash,
        hotspot_id=request.hotspot_id,
        replacement_point=request.replacement_point,
        decision=decision,
        reviewer="candidate-reviewer",
        reason="The bounded source-only change is approved for controlled packaging.",
        review_evidence_uri="evidence:///agent/reviews/000000000040.json",
        review_evidence_hash=_hash("d"),
        reviewed_at=FIXED_TIME,
    )


def _promotion() -> CandidateProposalPromotionReceipt:
    request = _request()
    proposal = _proposal()
    review = _review()
    return CandidateProposalPromotionReceipt(
        promotion_id=UUID("00000000-0000-0000-0000-000000000050"),
        idempotency_key="promote-proposal-000000000050",
        proposal_id=proposal.proposal_id,
        proposal_hash=candidate_proposal_hash(proposal),
        request_id=request.request_id,
        request_hash=candidate_generation_request_hash(request),
        generation_run_id=request.generation_run_id,
        patch_uri=proposal.patch_uri,
        patch_hash=proposal.patch_hash,
        normalized_patch_hash=proposal.normalized_patch_hash,
        baseline_epoch_id=request.baseline_epoch_id,
        baseline_source_hash=request.baseline_source_hash,
        hotspot_id=request.hotspot_id,
        replacement_point=request.replacement_point,
        review=review,
        review_record_hash=candidate_proposal_review_record_hash(review),
        candidate_id=UUID("00000000-0000-0000-0000-000000000051"),
        source_package_ref={
            "candidate_source_hash": _hash("e"),
            "source_package_hash": _hash("f"),
            "manifest_hash": _hash("0"),
            "manifest_schema_version": "m1-candidate-source-v1",
        },
        source_family_hash=_hash("1"),
        source_family_verification_evidence_uri=(
            "evidence:///m2a/business-family/verified.json"
        ),
        source_family_verification_evidence_hash=_hash("2"),
        source_family_verifier_provenance=AdapterProvenance(
            profile="m2a-family-contract-test",
            capability="business_candidate_family_verification",
            adapter_name="BusinessCandidateFamilyVerifier",
            adapter_version="m2a-business-candidate-family-v1",
            implementation_kind="fake",
        ),
        promoted_by="candidate-authority",
        promoted_at=FIXED_TIME,
        synthetic=True,
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
                "max_output_bytes_per_attempt": 200_000,
                "max_tokens_per_attempt": 20_000,
            },
            {
                "generator_id": "agent-b",
                "adapter_profile": "m2b-agent-b-v1",
                "max_attempts": 1,
                "max_proposals": 2,
                "timeout_seconds": 600,
                "max_output_bytes_per_attempt": 200_000,
                "max_tokens_per_attempt": 20_000,
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

    partial = _batch().model_dump(mode="json")
    partial.update(
        status="partial",
        error_code="bounded_partial_output",
        error_message="generator stopped after its bounded output limit",
    )
    assert CandidateProposalBatch.model_validate(partial).status == "partial"
    assert candidate_proposal_batch_hash(_batch()).startswith("sha256:")


def test_proposal_and_batch_freeze_full_request_hash() -> None:
    request = _request()
    proposal = _proposal()
    assert proposal.request_hash == candidate_generation_request_hash(request)
    assert _batch().request_hash == proposal.request_hash

    batch = _batch().model_dump(mode="json")
    batch["proposals"][0]["request_hash"] = _hash("a")
    with pytest.raises(ValidationError, match="cross-request Hash"):
        CandidateProposalBatch.model_validate(batch)

    changed_request = request.model_copy(update={"baseline_source_hash": _hash("b")})
    assert changed_request.request_id == request.request_id
    assert candidate_generation_request_hash(changed_request) != proposal.request_hash
    with pytest.raises(ValueError, match="frozen authority"):
        verify_candidate_proposal_review_record(
            changed_request,
            proposal,
            _raw_patch(),
            _review(),
        )


def test_normalized_patch_v1_has_one_public_stable_identity() -> None:
    with_format_noise = (
        b"diff --git a/sglang/runtime/operator.py b/sglang/runtime/operator.py\r\n"
        b"index abcdef0..1234567 100644\r\n"
        b"--- a/sglang/runtime/operator.py\t2026-08-31 08:00:00\r\n"
        b"+++ b/sglang/runtime/operator.py\t2026-08-31 08:00:01\r\n"
        b"@@ -1 +1 @@\r\n-return value\t \r\n+return value + 1\r\n\r\n"
    )
    canonical = _raw_patch()

    assert normalize_patch_v1(with_format_noise) == normalize_patch_v1(canonical)
    assert normalized_patch_hash_v1(with_format_noise) == normalized_patch_hash_v1(
        canonical
    )
    assert normalized_patch_hash_v1(_raw_patch(1)) != normalized_patch_hash_v1(
        canonical
    )
    changed_path = canonical.replace(b"operator.py", b"different.py")
    assert normalized_patch_hash_v1(changed_path) != normalized_patch_hash_v1(canonical)
    with pytest.raises(PatchIdentityError, match="strict UTF-8"):
        normalize_patch_v1(b"\xff")
    with pytest.raises(PatchIdentityError, match="NUL"):
        normalize_patch_v1(b"diff --git a/a.py b/a.py\x00")


def test_patch_identity_is_recomputed_from_reread_bytes_fail_closed() -> None:
    proposal = _proposal()
    verify_candidate_proposal_patch(proposal, _raw_patch())

    wrong_raw_hash = proposal.model_copy(update={"patch_hash": _hash("3")})
    with pytest.raises(ValueError, match="raw Patch Hash mismatch"):
        verify_candidate_proposal_patch(wrong_raw_hash, _raw_patch())

    wrong_normalized_hash = proposal.model_copy(
        update={"normalized_patch_hash": _hash("4")}
    )
    with pytest.raises(ValueError, match="normalized Patch Hash mismatch"):
        verify_candidate_proposal_patch(wrong_normalized_hash, _raw_patch())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("baseline_source_hash", _hash("5")),
        ("hotspot_id", UUID("00000000-0000-0000-0000-000000000099")),
        ("replacement_point", "sglang.runtime.other.forward"),
        ("patch_hash", _hash("6")),
    ],
)
def test_review_rejects_authority_or_patch_drift(field: str, value: object) -> None:
    review = _review().model_copy(update={field: value})
    with pytest.raises(ValueError, match="does not match"):
        verify_candidate_proposal_review_record(
            _request(),
            _proposal(),
            _raw_patch(),
            review,
        )


def test_review_and_promotion_bind_existing_m2a_authority() -> None:
    request = _request()
    proposal = _proposal()
    review = _review()
    receipt = _promotion()

    verify_candidate_proposal_review_record(request, proposal, _raw_patch(), review)
    verify_candidate_proposal_promotion_receipt(
        request,
        proposal,
        _raw_patch(),
        receipt,
    )
    assert isinstance(receipt.source_package_ref, CandidateSourcePackageRef)
    assert receipt.candidate_authority == "existing_m2a_source_package_and_family_only"
    assert receipt.formal_intake_allowed is False
    assert receipt.automatic_release_allowed is False
    assert candidate_proposal_promotion_receipt_hash(receipt).startswith("sha256:")


def test_promotion_requires_approved_review_and_family_verification() -> None:
    receipt = _promotion().model_dump(mode="json")
    receipt["review"]["decision"] = "rejected"
    with pytest.raises(ValidationError, match="requires an approved review"):
        CandidateProposalPromotionReceipt.model_validate(receipt)

    missing_review = _promotion().model_dump(mode="json")
    del missing_review["review"]
    with pytest.raises(ValidationError, match="review"):
        CandidateProposalPromotionReceipt.model_validate(missing_review)

    missing_family_evidence = _promotion().model_dump(mode="json")
    del missing_family_evidence["source_family_verification_evidence_hash"]
    with pytest.raises(ValidationError, match="source_family_verification_evidence_hash"):
        CandidateProposalPromotionReceipt.model_validate(missing_family_evidence)

    wrong_review_hash = _promotion().model_copy(update={"review_record_hash": _hash("7")})
    with pytest.raises(ValueError, match="Review Record Hash mismatch"):
        verify_candidate_proposal_promotion_receipt(
            _request(),
            _proposal(),
            _raw_patch(),
            wrong_review_hash,
        )


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
