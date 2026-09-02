# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib

from hcuopt.contracts.agent_v1 import (
    ApexGenerationPlan,
    CandidateGenerationRequest,
    CandidateProposal,
    CandidateProposalBatch,
    CandidateProposalPromotionReceipt,
    CandidateProposalReviewRecord,
    KnowledgeSnapshot,
)
from hcuopt.measurement.evidence import canonical_json_bytes

from .patch_identity import normalized_patch_hash_v1, raw_patch_hash_v1


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


def candidate_proposal_review_record_hash(
    review: CandidateProposalReviewRecord,
) -> str:
    return _canonical_hash(review)


def candidate_proposal_promotion_receipt_hash(
    receipt: CandidateProposalPromotionReceipt,
) -> str:
    return _canonical_hash(receipt)


def verify_candidate_proposal_patch(proposal: CandidateProposal, raw_patch: bytes) -> None:
    if raw_patch_hash_v1(raw_patch) != proposal.patch_hash:
        raise ValueError("Candidate Proposal raw Patch Hash mismatch")
    if normalized_patch_hash_v1(raw_patch) != proposal.normalized_patch_hash:
        raise ValueError("Candidate Proposal normalized Patch Hash mismatch")


def verify_candidate_proposal_review_record(
    request: CandidateGenerationRequest,
    proposal: CandidateProposal,
    raw_patch: bytes,
    review: CandidateProposalReviewRecord,
) -> None:
    request_hash = candidate_generation_request_hash(request)
    proposal_hash = candidate_proposal_hash(proposal)
    verify_candidate_proposal_patch(proposal, raw_patch)
    expected = (
        request.request_id,
        request_hash,
        request.generation_run_id,
        request.replacement_point,
        proposal.proposal_id,
        proposal_hash,
        proposal.patch_uri,
        proposal.patch_hash,
        proposal.normalized_patch_hash,
        request.baseline_epoch_id,
        request.baseline_source_hash,
        request.hotspot_id,
    )
    actual = (
        proposal.request_id,
        proposal.request_hash,
        proposal.generation_run_id,
        proposal.replacement_point,
        review.proposal_id,
        review.proposal_hash,
        review.patch_uri,
        review.patch_hash,
        review.normalized_patch_hash,
        review.baseline_epoch_id,
        review.baseline_source_hash,
        review.hotspot_id,
    )
    if actual != expected:
        raise ValueError("Candidate Proposal review does not match frozen authority")
    review_request_binding = (
        review.request_id,
        review.request_hash,
        review.generation_run_id,
        review.replacement_point,
    )
    if review_request_binding != expected[:4]:
        raise ValueError("Candidate Proposal review does not match its Request")


def verify_candidate_proposal_promotion_receipt(
    request: CandidateGenerationRequest,
    proposal: CandidateProposal,
    raw_patch: bytes,
    receipt: CandidateProposalPromotionReceipt,
) -> None:
    review = receipt.review
    verify_candidate_proposal_review_record(request, proposal, raw_patch, review)
    if review.decision != "approved":
        raise ValueError("Candidate Proposal promotion requires approved human review")
    if receipt.review_record_hash != candidate_proposal_review_record_hash(review):
        raise ValueError("Candidate Proposal promotion Review Record Hash mismatch")
    expected = (
        review.proposal_id,
        review.proposal_hash,
        review.request_id,
        review.request_hash,
        review.generation_run_id,
        review.patch_uri,
        review.patch_hash,
        review.normalized_patch_hash,
        review.baseline_epoch_id,
        review.baseline_source_hash,
        review.hotspot_id,
        review.replacement_point,
    )
    actual = (
        receipt.proposal_id,
        receipt.proposal_hash,
        receipt.request_id,
        receipt.request_hash,
        receipt.generation_run_id,
        receipt.patch_uri,
        receipt.patch_hash,
        receipt.normalized_patch_hash,
        receipt.baseline_epoch_id,
        receipt.baseline_source_hash,
        receipt.hotspot_id,
        receipt.replacement_point,
    )
    if actual != expected:
        raise ValueError("Candidate Proposal promotion does not match approved authority")


def candidate_proposal_batch_hash(batch: CandidateProposalBatch) -> str:
    value = batch.model_dump(mode="json")
    value["proposals"] = sorted(
        value["proposals"],
        key=lambda item: (item["ordinal"], item["proposal_id"]),
    )
    return _canonical_hash(value)
