# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from hcuopt.adapters.agent_generator import (
    CandidateProposalBatchStore,
    ProposalPatchStore,
)
from hcuopt.adapters.agent_promotion import (
    BaselineOverlaySource,
    CandidateSourcePackagePublisher,
    PreparedCandidatePackage,
    ProposalDecisionStore,
    ProposalPromotionService,
    ProposalReviewAuthority,
    apply_single_file_unified_patch,
)
from hcuopt.adapters.business_candidate_family import BusinessCandidateFamilyVerifier
from hcuopt.agent.authority import (
    actual_usage_for,
    build_generation_run_start,
    finalize_proposal_dispositions,
    generation_plan_id_for,
    generation_run_id_for,
    proposal_refs_for_batch,
)
from hcuopt.agent.identity import (
    candidate_generation_request_hash,
    candidate_proposal_promotion_receipt_hash,
)
from hcuopt.contracts.agent_v1 import (
    ApexGenerationPlan,
    CandidateGenerationRequest,
    CandidateProposalBatch,
    GenerationBudgetUsage,
    GenerationRun,
    GenerationRunStartRequest,
    GenerationRunStatusView,
    GeneratorAttempt,
)
from hcuopt.contracts.m2_candidate_family_v1 import BusinessCandidateFamilyManifest
from hcuopt.contracts.platform_v1 import AdapterProvenance, SourceSnapshot
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path

NOW = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
BASELINE_PATH = "sglang/runtime/operator.py"
REPLACEMENT_POINT = "sglang.runtime.operator.forward"
MOUNT_TARGET = "/opt/hcuopt/overlay/sglang/runtime/operator.py"
BASELINE = b"def forward(value):\n    return value\n"
STORE_ID = "m2b-promotion-source-store-v1"


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _patch(delta: int, *, path: str = BASELINE_PATH) -> bytes:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,2 +1,2 @@\n"
        " def forward(value):\n"
        "-    return value\n"
        f"+    return value + {delta}\n"
    ).encode()


@dataclass(frozen=True, slots=True)
class PromotionFixture:
    status: GenerationRunStatusView
    patch_store: ProposalPatchStore
    batch_store: CandidateProposalBatchStore
    decision_store: ProposalDecisionStore
    authority: ProposalReviewAuthority
    publisher: CandidateSourcePackagePublisher
    service: ProposalPromotionService
    baseline: BaselineOverlaySource
    candidate_output_dir: Path


class CopySourceManager:
    """Test Source Manager preserving the same full-tree hash contract as F1-C."""

    def __init__(self) -> None:
        self.provenance = AdapterProvenance(
            profile="m2b-copy-source-test-v1",
            capability="source_manager",
            adapter_name=type(self).__name__,
            adapter_version="1.0.0",
            implementation_kind="real",
        )

    def create_candidate(
        self,
        baseline: SourceSnapshot,
        candidate_id: UUID,
        output_dir: Path,
    ) -> SourceSnapshot:
        source = file_uri_to_path(baseline.worktree_uri)
        destination = output_dir / "worktrees" / str(candidate_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, symlinks=True)
        return SourceSnapshot(
            kind="candidate",
            repository=baseline.repository,
            commit=baseline.commit,
            tree_hash=baseline.tree_hash,
            source_hash=canonical_source_hash(destination),
            worktree_uri=destination.resolve(strict=True).as_uri(),
            clean=True,
            parent_snapshot_id=baseline.snapshot_id,
            created_at=NOW,
        )

    def remove_candidate(
        self,
        baseline: SourceSnapshot,
        candidate: SourceSnapshot,
        output_dir: Path,
    ) -> None:
        del output_dir
        shutil.rmtree(file_uri_to_path(candidate.worktree_uri))
        if canonical_source_hash(file_uri_to_path(baseline.worktree_uri)) != (
            baseline.source_hash
        ):
            raise SourceArtifactError("test Baseline changed during Candidate cleanup")


def _running(attempt: GeneratorAttempt, ordinal: int) -> GeneratorAttempt:
    return GeneratorAttempt.model_validate(
        {
            **attempt.model_dump(mode="json"),
            "state": "running",
            "worker_id": f"proposal-worker-{ordinal}",
            "claim_token": str(UUID(int=8_000 + ordinal)),
            "lease_expires_at": NOW + timedelta(minutes=1),
            "started_at": NOW,
            "updated_at": NOW,
            "version": 2,
        }
    )


def _fixture(
    tmp_path: Path,
    *,
    ordinal: int = 1,
    real: bool = True,
    path: str = BASELINE_PATH,
) -> PromotionFixture:
    idempotency_key = f"m2b-promotion-fixture-{ordinal}"
    run_id = generation_run_id_for(idempotency_key)
    baseline_root = tmp_path / "baseline-source"
    baseline_file = baseline_root / path
    baseline_file.parent.mkdir(parents=True, exist_ok=True)
    baseline_file.write_bytes(BASELINE)
    baseline_source_hash = canonical_source_hash(baseline_root)
    baseline_snapshot = SourceSnapshot(
        snapshot_id=UUID(int=9_000 + ordinal),
        kind="baseline",
        repository="https://github.com/example/sglang.git",
        commit="1" * 40,
        tree_hash="2" * 40,
        source_hash=baseline_source_hash,
        worktree_uri=baseline_root.resolve(strict=True).as_uri(),
        clean=True,
        created_at=NOW,
    )
    request = CandidateGenerationRequest(
        request_id=UUID(int=1_000 + ordinal),
        generation_run_id=run_id,
        target_snapshot_id=UUID(int=2_001),
        stage0_run_id=UUID(int=2_002),
        baseline_epoch_id=UUID(int=2_003),
        baseline_source_hash=baseline_source_hash,
        hotspot_id=UUID(int=2_004),
        replacement_point=REPLACEMENT_POINT,
        workload_id="m2b-promotion-fixture",
        workload_hash=_sha256(b"workload"),
        configuration_hash=_sha256(b"configuration"),
        image_digest=_sha256(b"image"),
        profiler_evidence_uri="evidence:///m2b/profile.json",
        profiler_evidence_hash=_sha256(b"profile"),
        knowledge_snapshot_id=UUID(int=2_005),
        knowledge_snapshot_hash=_sha256(b"knowledge"),
        max_proposals=1,
    )
    profile = "m2b-real-generator-v1" if real else "m2b-fake-generator-v1"
    plan = ApexGenerationPlan(
        plan_id=generation_plan_id_for(run_id),
        generation_run_id=run_id,
        request_id=request.request_id,
        request_hash=candidate_generation_request_hash(request),
        generators=(
            {
                "generator_id": f"agent-{ordinal}",
                "adapter_profile": profile,
                "max_attempts": 1,
                "max_proposals": 1,
                "timeout_seconds": 10,
                "max_output_bytes_per_attempt": 20_000,
                "max_tokens_per_attempt": 2_000,
            },
        ),
        max_concurrency=1,
        budget={
            "max_generator_attempts": 1,
            "max_wall_seconds": 10,
            "max_total_output_bytes": 20_000,
            "max_total_tokens": 2_000,
            "max_proposals": 1,
        },
        created_by="apex-control-plane",
        created_at=NOW,
    )
    start = GenerationRunStartRequest(
        request=request,
        plan=plan,
        actor="proposal-promotion-test",
        idempotency_key=idempotency_key,
    )
    run, attempts, _ledger = build_generation_run_start(start, created_at=NOW)
    running = _running(attempts[0], ordinal)

    proposal_root = tmp_path / "proposal-store"
    patch_store = ProposalPatchStore(proposal_root, profile="m2b-c-v1")
    batch_store = CandidateProposalBatchStore(proposal_root, profile="m2b-c-v1")
    stored_patch = patch_store.publish(_patch(ordinal, path=path))
    raw_output = f'{{"proposal": {ordinal}}}\n'.encode()
    raw_output_path = proposal_root / "raw" / f"{ordinal}.json"
    raw_output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_output_path.write_bytes(raw_output)
    provenance = AdapterProvenance(
        profile=profile,
        capability="candidate_proposal_generation",
        adapter_name="BoundedTestGenerator",
        adapter_version="1.0.0",
        implementation_kind="real" if real else "fake",
    )
    batch = CandidateProposalBatch(
        batch_id=UUID(int=3_000 + ordinal),
        request_id=request.request_id,
        request_hash=candidate_generation_request_hash(request),
        generation_run_id=run_id,
        generator_id=plan.generators[0].generator_id,
        adapter_provenance=provenance,
        status="succeeded",
        proposals=(
            {
                "proposal_id": UUID(int=4_000 + ordinal),
                "request_id": request.request_id,
                "request_hash": candidate_generation_request_hash(request),
                "generation_run_id": run_id,
                "generator_id": plan.generators[0].generator_id,
                "ordinal": 0,
                "optimization_intent": f"remove redundant operation {ordinal}",
                "rationale": "Exercise the bounded proposal promotion path.",
                "risk_summary": "Independent correctness review remains mandatory.",
                "patch_uri": stored_patch.uri,
                "patch_hash": stored_patch.patch_hash,
                "normalized_patch_hash": stored_patch.normalized_patch_hash,
                "touched_paths": (path,),
                "replacement_point": REPLACEMENT_POINT,
            },
        ),
        raw_output_uri=raw_output_path.resolve(strict=True).as_uri(),
        raw_output_hash=_sha256(raw_output),
        output_bytes=len(raw_output),
        token_count=0,
        attempt_count=1,
        wall_seconds=0.1,
        started_at=NOW,
        finished_at=NOW + timedelta(milliseconds=100),
        synthetic=not real,
    )
    stored_batch = batch_store.publish(batch)
    references = finalize_proposal_dispositions(
        proposal_refs_for_batch(run, running, plan.generators[0], batch)
    )
    usage = actual_usage_for(batch)
    succeeded = GeneratorAttempt.model_validate(
        {
            **running.model_dump(mode="json"),
            "state": "succeeded",
            "actual": usage.model_dump(mode="json"),
            "batch_id": str(batch.batch_id),
            "batch_hash": stored_batch.batch_hash,
            "batch_status": batch.status,
            "raw_output_uri": batch.raw_output_uri,
            "raw_output_hash": batch.raw_output_hash,
            "adapter_provenance": provenance.model_dump(mode="json"),
            "finished_at": NOW + timedelta(milliseconds=100),
            "updated_at": NOW + timedelta(milliseconds=100),
            "version": 3,
        }
    )
    reviewable_run = GenerationRun.model_validate(
        {
            **run.model_dump(mode="json"),
            "state": "awaiting_review",
            "terminal_attempt_count": 1,
            "terminal_generator_count": 1,
            "proposal_count": 1,
            "retained_proposal_count": 1,
            "budget_reserved": GenerationBudgetUsage().model_dump(mode="json"),
            "budget_consumed": usage.model_dump(mode="json"),
            "updated_at": NOW + timedelta(milliseconds=100),
            "version": 2,
        }
    )
    status = GenerationRunStatusView(
        run=reviewable_run,
        attempts=(succeeded,),
        proposals=references,
    )
    decision_store = ProposalDecisionStore(tmp_path / "decision-store")
    authority = ProposalReviewAuthority(
        patch_store=patch_store,
        batch_store=batch_store,
        decision_store=decision_store,
    )
    publisher = CandidateSourcePackagePublisher(
        tmp_path / "source-packages",
        profile="m2b-promotion-v1",
        source_manager=CopySourceManager(),
        allowed_overlay_roots=("sglang",),
        approved_mount_targets={REPLACEMENT_POINT: MOUNT_TARGET},
    )
    service = ProposalPromotionService(
        review_authority=authority,
        package_publisher=publisher,
        decision_store=decision_store,
    )
    return PromotionFixture(
        status=status,
        patch_store=patch_store,
        batch_store=batch_store,
        decision_store=decision_store,
        authority=authority,
        publisher=publisher,
        service=service,
        baseline=BaselineOverlaySource(
            snapshot=baseline_snapshot,
            path=path,
        ),
        candidate_output_dir=tmp_path / "candidate-work",
    )


def _review(fixture: PromotionFixture, *, decision: str = "approved"):
    proposal = fixture.status.proposals[0]
    return fixture.authority.review(
        fixture.status,
        proposal.proposal_id,
        decision=decision,
        reviewer="proposal-reviewer",
        reason="The bounded source-only change was reviewed.",
        review_evidence=b'{"review": "bounded-source-only"}\n',
        idempotency_key=f"review-{proposal.proposal_id}",
        reviewed_at=NOW,
    )


def _prepare(
    fixture: PromotionFixture,
    *,
    candidate_id: UUID,
) -> PreparedCandidatePackage:
    review = _review(fixture)
    return fixture.service.prepare(
        fixture.status,
        fixture.status.proposals[0].proposal_id,
        review.review_id,
        baseline=fixture.baseline,
        candidate_id=candidate_id,
        candidate_output_dir=fixture.candidate_output_dir,
    )


def test_single_file_patch_applies_and_rejects_baseline_or_path_drift() -> None:
    assert apply_single_file_unified_patch(
        BASELINE,
        _patch(1),
        expected_path=BASELINE_PATH,
    ) == b"def forward(value):\n    return value + 1\n"

    with pytest.raises(SourceArtifactError, match="deletion differs"):
        apply_single_file_unified_patch(
            BASELINE.replace(b"return value", b"return changed"),
            _patch(1),
            expected_path=BASELINE_PATH,
        )
    with pytest.raises(SourceArtifactError, match="path differs"):
        apply_single_file_unified_patch(
            BASELINE,
            _patch(1),
            expected_path="sglang/runtime/other.py",
        )


@pytest.mark.parametrize("disposition", ["pending", "duplicate"])
def test_review_rejects_unretained_proposal(
    tmp_path: Path,
    disposition: str,
) -> None:
    fixture = _fixture(tmp_path)
    reference = fixture.status.proposals[0]
    update: dict[str, object] = {"disposition": disposition}
    if disposition == "duplicate":
        update["duplicate_of_proposal_id"] = UUID(int=99_999)
    changed = reference.model_copy(update=update)
    run = fixture.status.run.model_copy(update={"retained_proposal_count": 0})
    status = fixture.status.model_copy(update={"run": run, "proposals": (changed,)})

    with pytest.raises(SourceArtifactError, match="retained Proposal"):
        fixture.authority.resolve(status, reference.proposal_id)


def test_review_rejects_run_request_and_reference_authority_drift(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    proposal_id = fixture.status.proposals[0].proposal_id
    running = fixture.status.run.model_copy(update={"state": "running"})
    with pytest.raises(SourceArtifactError, match="awaiting_review"):
        fixture.authority.resolve(
            fixture.status.model_copy(update={"run": running}),
            proposal_id,
        )

    changed_request = fixture.status.run.request.model_copy(
        update={"baseline_source_hash": _sha256(b"changed")}
    )
    drifted_run = fixture.status.run.model_copy(update={"request": changed_request})
    with pytest.raises(SourceArtifactError, match="Request authority"):
        fixture.authority.resolve(
            fixture.status.model_copy(update={"run": drifted_run}),
            proposal_id,
        )

    changed_ref = fixture.status.proposals[0].model_copy(
        update={"proposal_hash": _sha256(b"changed-ref")}
    )
    with pytest.raises(SourceArtifactError, match="Ref changed"):
        fixture.authority.resolve(
            fixture.status.model_copy(update={"proposals": (changed_ref,)}),
            proposal_id,
        )

    changed_plan = fixture.status.run.model_copy(
        update={"plan_hash": _sha256(b"changed-plan")}
    )
    with pytest.raises(SourceArtifactError, match="Plan authority"):
        fixture.authority.resolve(
            fixture.status.model_copy(update={"run": changed_plan}),
            proposal_id,
        )

    changed_attempt = fixture.status.attempts[0].model_copy(
        update={"raw_output_hash": _sha256(b"changed-output")}
    )
    with pytest.raises(SourceArtifactError, match="settlement differs"):
        fixture.authority.resolve(
            fixture.status.model_copy(update={"attempts": (changed_attempt,)}),
            proposal_id,
        )

    changed_counts = fixture.status.run.model_copy(update={"proposal_count": 2})
    with pytest.raises(SourceArtifactError, match="Status counts"):
        fixture.authority.resolve(
            fixture.status.model_copy(update={"run": changed_counts}),
            proposal_id,
        )


def test_rejected_or_synthetic_proposal_cannot_publish_business_package(
    tmp_path: Path,
) -> None:
    rejected = _fixture(tmp_path / "rejected")
    review = _review(rejected, decision="rejected")
    with pytest.raises(SourceArtifactError, match="rejected"):
        rejected.service.prepare(
            rejected.status,
            rejected.status.proposals[0].proposal_id,
            review.review_id,
            baseline=rejected.baseline,
            candidate_id=UUID(int=5_001),
            candidate_output_dir=rejected.candidate_output_dir,
        )

    synthetic = _fixture(tmp_path / "synthetic", real=False)
    review = _review(synthetic)
    with pytest.raises(SourceArtifactError, match="synthetic or fake"):
        synthetic.service.prepare(
            synthetic.status,
            synthetic.status.proposals[0].proposal_id,
            review.review_id,
            baseline=synthetic.baseline,
            candidate_id=UUID(int=5_002),
            candidate_output_dir=synthetic.candidate_output_dir,
        )


def test_review_store_rejects_post_publication_tampering_and_key_reuse(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    review = _review(fixture, decision="rejected")
    review_path = fixture.decision_store.root / "reviews" / f"{review.review_id}.json"
    review_path.write_bytes(
        canonical_json_bytes(review.model_copy(update={"decision": "approved"}))
    )

    with pytest.raises(SourceArtifactError, match="review content changed"):
        fixture.decision_store.load_review(review.review_id)

    review_path.write_bytes(canonical_json_bytes(review))
    with pytest.raises(SourceArtifactError, match="different bytes"):
        fixture.authority.review(
            fixture.status,
            fixture.status.proposals[0].proposal_id,
            decision="rejected",
            reviewer="proposal-reviewer",
            reason="A different decision under the same idempotency key is forbidden.",
            review_evidence=b'{"review": "bounded-source-only"}\n',
            idempotency_key=f"review-{fixture.status.proposals[0].proposal_id}",
            reviewed_at=NOW,
        )


def test_publisher_rejects_unapproved_overlay_root(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, path="outside/runtime/operator.py")
    review = _review(fixture)

    with pytest.raises(SourceArtifactError, match="unapproved Overlay root"):
        fixture.service.prepare(
            fixture.status,
            fixture.status.proposals[0].proposal_id,
            review.review_id,
            baseline=fixture.baseline,
            candidate_id=UUID(int=5_003),
            candidate_output_dir=fixture.candidate_output_dir,
        )


def test_publisher_rereads_and_rejects_baseline_worktree_drift(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    review = _review(fixture)
    baseline_root = file_uri_to_path(fixture.baseline.snapshot.worktree_uri)
    (baseline_root / fixture.baseline.path).write_bytes(
        b"def forward(value):\n    return changed\n"
    )

    with pytest.raises(SourceArtifactError, match="authority drifted"):
        fixture.service.prepare(
            fixture.status,
            fixture.status.proposals[0].proposal_id,
            review.review_id,
            baseline=fixture.baseline,
            candidate_id=UUID(int=5_004),
            candidate_output_dir=fixture.candidate_output_dir,
        )


def test_two_real_packages_verify_and_produce_replayable_promotion_receipt(
    tmp_path: Path,
) -> None:
    first_fixture = _fixture(tmp_path, ordinal=1)
    second_fixture = _fixture(tmp_path, ordinal=2)
    first = _prepare(first_fixture, candidate_id=UUID(int=6_001))
    second = _prepare(second_fixture, candidate_id=UUID(int=6_002))
    replay_root = tmp_path / "replayed-first-candidate"
    shutil.copytree(
        file_uri_to_path(first_fixture.baseline.snapshot.worktree_uri),
        replay_root,
        symlinks=True,
    )
    replayed_package = first_fixture.publisher.source_packages.read(
        candidate_source_hash=first.source_package_ref.candidate_source_hash
    )
    first_fixture.publisher.source_packages.apply(replayed_package, replay_root)
    assert canonical_source_hash(replay_root) == (
        first.source_package_ref.candidate_source_hash
    )
    assert not any(first_fixture.candidate_output_dir.joinpath("worktrees").iterdir())
    request = first.resolved.status.run.request
    store_hash = _sha256(b"deployment-owned-source-store")
    family = BusinessCandidateFamilyManifest(
        family_id="m2b-promotion-family-v1",
        source_package_store_id=STORE_ID,
        source_package_store_hash=store_hash,
        target_snapshot_id=request.target_snapshot_id,
        stage0_run_id=request.stage0_run_id,
        baseline_epoch_id=request.baseline_epoch_id,
        baseline_source_hash=request.baseline_source_hash,
        hotspot_id=request.hotspot_id,
        replacement_point=request.replacement_point,
        profiler_evidence_uri=request.profiler_evidence_uri,
        profiler_evidence_hash=request.profiler_evidence_hash,
        overlay_mount_target=MOUNT_TARGET,
        overlay_file_path=BASELINE_PATH,
        members=(
            {
                "candidate_id": first.candidate_id,
                "source_package_ref": first.source_package_ref,
                "optimization_intent": first.resolved.proposal.optimization_intent,
            },
            {
                "candidate_id": second.candidate_id,
                "source_package_ref": second.source_package_ref,
                "optimization_intent": second.resolved.proposal.optimization_intent,
            },
        ),
        reviewed_by="candidate-family-reviewer",
        reviewed_at=NOW,
    )
    verifier = BusinessCandidateFamilyVerifier(
        first_fixture.publisher.source_packages,
        store_id=STORE_ID,
        store_hash=store_hash,
    )

    receipt = first_fixture.service.finalize(
        first,
        family,
        verifier,
        promoted_by="proposal-promotion-authority",
        promoted_at=NOW,
        idempotency_key="promote-first-proposal-v1",
    )
    reread = first_fixture.decision_store.load_receipt(receipt.promotion_id)
    evidence = first_fixture.decision_store.read_evidence(
        reread.source_family_verification_evidence_uri,
        expected_hash=reread.source_family_verification_evidence_hash,
    )

    assert candidate_proposal_promotion_receipt_hash(reread) == (
        candidate_proposal_promotion_receipt_hash(receipt)
    )
    assert reread.source_package_ref == first.source_package_ref
    assert reread.source_family_verifier_provenance.implementation_kind == "real"
    assert b"m2b-source-family-verification-evidence-v1" in evidence
    assert reread.formal_intake_allowed is False
    assert reread.automatic_release_allowed is False
    assert reread.performance_conclusion == "not_measured"

    receipt_path = (
        first_fixture.decision_store.root
        / "promotions"
        / f"{receipt.promotion_id}.json"
    )
    receipt_path.write_bytes(
        canonical_json_bytes(receipt.model_copy(update={"promoted_by": "tampered"}))
    )
    with pytest.raises(SourceArtifactError, match="promotion content changed"):
        first_fixture.decision_store.load_receipt(receipt.promotion_id)
    receipt_path.write_bytes(canonical_json_bytes(receipt))

    review_evidence_path = file_uri_to_path(receipt.review.review_evidence_uri)
    review_evidence = review_evidence_path.read_bytes()
    review_evidence_path.write_bytes(review_evidence + b"tampered\n")
    with pytest.raises(SourceArtifactError, match="evidence Hash changed"):
        first_fixture.decision_store.load_receipt(receipt.promotion_id)
    review_evidence_path.write_bytes(review_evidence)

    family_file = file_uri_to_path(reread.source_family_verification_evidence_uri)
    family_file.write_bytes(evidence + b"tampered\n")
    with pytest.raises(SourceArtifactError, match="Hash changed"):
        first_fixture.decision_store.read_evidence(
            reread.source_family_verification_evidence_uri,
            expected_hash=reread.source_family_verification_evidence_hash,
        )


def test_family_rejects_duplicate_or_authority_drift_before_receipt(tmp_path: Path) -> None:
    first_fixture = _fixture(tmp_path, ordinal=1)
    second_fixture = _fixture(tmp_path, ordinal=2)
    first = _prepare(first_fixture, candidate_id=UUID(int=7_001))
    second = _prepare(second_fixture, candidate_id=UUID(int=7_002))
    request = first.resolved.status.run.request
    store_hash = _sha256(b"deployment-owned-source-store")
    base = {
        "family_id": "m2b-promotion-family-drift-v1",
        "source_package_store_id": STORE_ID,
        "source_package_store_hash": store_hash,
        "target_snapshot_id": request.target_snapshot_id,
        "stage0_run_id": request.stage0_run_id,
        "baseline_epoch_id": request.baseline_epoch_id,
        "baseline_source_hash": request.baseline_source_hash,
        "hotspot_id": request.hotspot_id,
        "replacement_point": request.replacement_point,
        "profiler_evidence_uri": request.profiler_evidence_uri,
        "profiler_evidence_hash": request.profiler_evidence_hash,
        "overlay_mount_target": MOUNT_TARGET,
        "overlay_file_path": BASELINE_PATH,
        "members": (
            {
                "candidate_id": first.candidate_id,
                "source_package_ref": first.source_package_ref,
                "optimization_intent": first.resolved.proposal.optimization_intent,
            },
            {
                "candidate_id": second.candidate_id,
                "source_package_ref": second.source_package_ref,
                "optimization_intent": second.resolved.proposal.optimization_intent,
            },
        ),
        "reviewed_by": "candidate-family-reviewer",
        "reviewed_at": NOW,
    }
    verifier = BusinessCandidateFamilyVerifier(
        first_fixture.publisher.source_packages,
        store_id=STORE_ID,
        store_hash=store_hash,
    )
    drifted = BusinessCandidateFamilyManifest.model_validate(
        {**base, "baseline_source_hash": _sha256(b"different-baseline")}
    )
    with pytest.raises(SourceArtifactError, match="approved Proposal authority"):
        first_fixture.service.finalize(
            first,
            drifted,
            verifier,
            promoted_by="proposal-promotion-authority",
            promoted_at=NOW,
            idempotency_key="reject-drifted-family-v1",
        )

    duplicated = dict(base)
    duplicated["members"] = (base["members"][0], base["members"][0])
    with pytest.raises(ValueError, match="duplicate Candidate identity"):
        BusinessCandidateFamilyManifest.model_validate(duplicated)
