# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from hcuopt.adapters.agent_generator import (
    MAX_PROPOSAL_PATCH_BYTES,
    CandidateProposalBatchStore,
    ProposalPatchStore,
    _publish_once,
    _read_regular,
)
from hcuopt.adapters.business_candidate_family import BusinessCandidateFamilyVerifier
from hcuopt.adapters.m2_candidate import candidate_source_package_hash
from hcuopt.adapters.manual_candidate import CandidateSourcePackageStore
from hcuopt.agent.authority import proposal_refs_for_batch
from hcuopt.agent.identity import (
    apex_generation_plan_hash,
    candidate_generation_request_hash,
    candidate_proposal_hash,
    candidate_proposal_promotion_receipt_hash,
    candidate_proposal_review_record_hash,
    verify_candidate_proposal_promotion_receipt,
    verify_candidate_proposal_review_record,
)
from hcuopt.contracts.agent_v1 import (
    CandidateProposal,
    CandidateProposalBatch,
    CandidateProposalPromotionReceipt,
    CandidateProposalRef,
    CandidateProposalReviewRecord,
    GenerationRunStatusView,
    GeneratorAttempt,
)
from hcuopt.contracts.m1 import CandidateOverlayFile, CandidateSourcePackageManifest
from hcuopt.contracts.m2 import CandidateSourcePackageRef
from hcuopt.contracts.m2_candidate_family_v1 import BusinessCandidateFamilyManifest
from hcuopt.contracts.platform_v1 import SourceSnapshot
from hcuopt.domain.enums import ManualCandidateKind
from hcuopt.domain.errors import SourceArtifactError, SourceIntegrityError
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path

MAX_DECISION_BYTES = 4 * 1024 * 1024
_HUNK = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?(?:\r?\n)?$"
)


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _model_path(root: Path, namespace: str, identity: UUID) -> Path:
    return root / namespace / f"{identity}.json"


@dataclass(frozen=True, slots=True)
class PublishedDecisionEvidence:
    uri: str
    content_hash: str


class ProposalDecisionStore:
    """Immutable review, promotion and verifier evidence owned by deployment."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def publish_evidence(
        self,
        evidence_kind: str,
        payload: bytes,
    ) -> PublishedDecisionEvidence:
        if (
            not evidence_kind
            or not evidence_kind.replace("-", "").replace("_", "").isalnum()
        ):
            raise SourceArtifactError("Proposal evidence kind is invalid")
        if len(payload) > MAX_DECISION_BYTES:
            raise SourceArtifactError("Proposal decision evidence exceeds its size limit")
        if not payload:
            raise SourceArtifactError("Proposal decision evidence cannot be empty")
        digest = _sha256(payload)
        value = digest.removeprefix("sha256:")
        path = (
            self.root
            / "evidence"
            / evidence_kind
            / "sha256"
            / value[:2]
            / value[2:]
            / "evidence.json"
        )
        _publish_once(path, payload)
        published = PublishedDecisionEvidence(
            uri=path.resolve(strict=True).as_uri(),
            content_hash=digest,
        )
        self.read_evidence(published.uri, expected_hash=published.content_hash)
        return published

    def read_evidence(self, uri: str, *, expected_hash: str) -> bytes:
        path = file_uri_to_path(uri).resolve(strict=True)
        evidence_root = (self.root / "evidence").resolve(strict=True)
        try:
            path.relative_to(evidence_root)
        except ValueError as error:
            raise SourceArtifactError(
                "Proposal decision evidence is outside the deployment-owned Store"
            ) from error
        payload = _read_regular(path, maximum_bytes=MAX_DECISION_BYTES)
        if _sha256(payload) != expected_hash:
            raise SourceArtifactError("Proposal decision evidence Hash changed in Store")
        return payload

    def publish_review(
        self,
        review: CandidateProposalReviewRecord,
    ) -> CandidateProposalReviewRecord:
        path = _model_path(self.root, "reviews", review.review_id)
        _publish_once(path, canonical_json_bytes(review))
        return self.load_review(review.review_id)

    def load_review(self, review_id: UUID) -> CandidateProposalReviewRecord:
        path = _model_path(self.root, "reviews", review_id)
        payload = _read_regular(path, maximum_bytes=MAX_DECISION_BYTES)
        review = CandidateProposalReviewRecord.model_validate_json(payload)
        if review.review_id != review_id:
            raise SourceArtifactError("Candidate Proposal review identity changed in Store")
        return review

    def publish_receipt(
        self,
        receipt: CandidateProposalPromotionReceipt,
    ) -> CandidateProposalPromotionReceipt:
        path = _model_path(self.root, "promotions", receipt.promotion_id)
        _publish_once(path, canonical_json_bytes(receipt))
        return self.load_receipt(receipt.promotion_id)

    def load_receipt(self, promotion_id: UUID) -> CandidateProposalPromotionReceipt:
        path = _model_path(self.root, "promotions", promotion_id)
        payload = _read_regular(path, maximum_bytes=MAX_DECISION_BYTES)
        receipt = CandidateProposalPromotionReceipt.model_validate_json(payload)
        if receipt.promotion_id != promotion_id:
            raise SourceArtifactError("Candidate Proposal promotion identity changed in Store")
        return receipt


@dataclass(frozen=True, slots=True)
class ResolvedRetainedProposal:
    status: GenerationRunStatusView
    attempt: GeneratorAttempt
    reference: CandidateProposalRef
    batch: CandidateProposalBatch
    proposal: CandidateProposal
    raw_patch: bytes


class ProposalReviewAuthority:
    """C rereads A authority and proposal bytes before recording a human decision."""

    def __init__(
        self,
        *,
        patch_store: ProposalPatchStore,
        batch_store: CandidateProposalBatchStore,
        decision_store: ProposalDecisionStore,
    ) -> None:
        self.patch_store = patch_store
        self.batch_store = batch_store
        self.decision_store = decision_store

    def resolve(
        self,
        status: GenerationRunStatusView,
        proposal_id: UUID,
    ) -> ResolvedRetainedProposal:
        run = status.run
        request_hash = candidate_generation_request_hash(run.request)
        if run.state != "awaiting_review":
            raise SourceArtifactError(
                "Candidate Proposal review requires an awaiting_review Generation Run"
            )
        if run.request_hash != request_hash or run.plan.request_hash != request_hash:
            raise SourceArtifactError("Generation Request authority changed before review")
        if run.plan_hash != apex_generation_plan_hash(run.plan):
            raise SourceArtifactError("Apex Generation Plan authority changed before review")
        references = [item for item in status.proposals if item.proposal_id == proposal_id]
        if len(references) != 1 or references[0].disposition != "retained":
            raise SourceArtifactError(
                "Candidate Proposal review requires one retained Proposal authority"
            )
        reference = references[0]
        attempts = [item for item in status.attempts if item.attempt_id == reference.attempt_id]
        if len(attempts) != 1:
            raise SourceArtifactError("Candidate Proposal Attempt authority is incomplete")
        attempt = attempts[0]
        if (
            attempt.state != "succeeded"
            or attempt.batch_id != reference.batch_id
            or attempt.batch_hash is None
        ):
            raise SourceArtifactError("Candidate Proposal Attempt is not reviewable")
        stored_batch = self.batch_store.load(
            attempt.batch_hash,
            expected_batch_id=reference.batch_id,
        )
        batch = stored_batch.batch
        if attempt.generator_ordinal >= len(run.plan.generators):
            raise SourceArtifactError("Candidate Proposal Attempt generator is outside its Plan")
        generator = run.plan.generators[attempt.generator_ordinal]
        if (
            attempt.batch_status != batch.status
            or attempt.raw_output_uri != batch.raw_output_uri
            or attempt.raw_output_hash != batch.raw_output_hash
            or attempt.adapter_provenance != batch.adapter_provenance
        ):
            raise SourceArtifactError(
                "Candidate Proposal Attempt settlement differs from its frozen Batch"
            )
        expected_refs = proposal_refs_for_batch(run, attempt, generator, batch)
        expected_refs = [item for item in expected_refs if item.proposal_id == proposal_id]
        if len(expected_refs) != 1:
            raise SourceArtifactError("Candidate Proposal is absent from its frozen Batch")
        expected_ref = expected_refs[0]
        immutable_fields = (
            "proposal_id",
            "proposal_hash",
            "generation_run_id",
            "request_id",
            "attempt_id",
            "batch_id",
            "generator_id",
            "generator_ordinal",
            "proposal_ordinal",
            "patch_uri",
            "patch_hash",
            "normalized_patch_hash",
        )
        if any(
            getattr(reference, field) != getattr(expected_ref, field)
            for field in immutable_fields
        ):
            raise SourceArtifactError("Candidate Proposal Ref changed after Batch settlement")
        proposals = [item for item in batch.proposals if item.proposal_id == proposal_id]
        if len(proposals) != 1:
            raise SourceArtifactError("Candidate Proposal identity is ambiguous in its Batch")
        proposal = proposals[0]
        if candidate_proposal_hash(proposal) != reference.proposal_hash:
            raise SourceArtifactError("Candidate Proposal content does not match its A reference")
        raw_patch = self.patch_store.read(
            proposal.patch_uri,
            expected_patch_hash=proposal.patch_hash,
            expected_normalized_patch_hash=proposal.normalized_patch_hash,
        )
        return ResolvedRetainedProposal(
            status=status,
            attempt=attempt,
            reference=reference,
            batch=batch,
            proposal=proposal,
            raw_patch=raw_patch,
        )

    def review(
        self,
        status: GenerationRunStatusView,
        proposal_id: UUID,
        *,
        decision: str,
        reviewer: str,
        reason: str,
        review_evidence: bytes,
        idempotency_key: str,
        reviewed_at: datetime,
    ) -> CandidateProposalReviewRecord:
        resolved = self.resolve(status, proposal_id)
        evidence = self.decision_store.publish_evidence(
            "proposal-review",
            review_evidence,
        )
        request = resolved.status.run.request
        proposal = resolved.proposal
        review = CandidateProposalReviewRecord(
            review_id=uuid5(
                NAMESPACE_URL,
                f"hcuopt:m2b-proposal-review:{idempotency_key}",
            ),
            idempotency_key=idempotency_key,
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
            reviewer=reviewer,
            reason=reason,
            review_evidence_uri=evidence.uri,
            review_evidence_hash=evidence.content_hash,
            reviewed_at=reviewed_at,
        )
        verify_candidate_proposal_review_record(
            request,
            proposal,
            resolved.raw_patch,
            review,
        )
        return self.decision_store.publish_review(review)


def apply_single_file_unified_patch(
    baseline: bytes,
    raw_patch: bytes,
    *,
    expected_path: str,
) -> bytes:
    """Apply one bounded text Patch without invoking a shell or importing Agent code."""

    try:
        baseline.decode("utf-8", errors="strict")
        patch_text = raw_patch.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise SourceArtifactError("Candidate promotion accepts strict UTF-8 source only") from error
    if b"\x00" in baseline or "\x00" in patch_text:
        raise SourceArtifactError("Candidate promotion rejects NUL source bytes")

    patch_lines = patch_text.splitlines(keepends=True)
    old_headers = [line for line in patch_lines if line.startswith("--- ")]
    new_headers = [line for line in patch_lines if line.startswith("+++ ")]
    if len(old_headers) != 1 or len(new_headers) != 1:
        raise SourceArtifactError("Candidate promotion requires one unified Patch file")

    def header_path(line: str, prefix: str) -> str:
        value = line[len(prefix) :].split("\t", 1)[0].strip()
        if value.startswith(("a/", "b/")):
            value = value[2:]
        return value

    if (
        header_path(old_headers[0], "--- ") != expected_path
        or header_path(new_headers[0], "+++ ") != expected_path
    ):
        raise SourceArtifactError("Candidate Patch path differs from its reviewed Overlay path")

    baseline_lines = baseline.splitlines(keepends=True)
    output: list[bytes] = []
    source_index = 0
    index = 0
    hunk_count = 0
    while index < len(patch_lines):
        match = _HUNK.match(patch_lines[index])
        if match is None:
            index += 1
            continue
        hunk_count += 1
        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        new_count = int(match.group(4) or "1")
        target_index = old_start - 1
        if target_index < source_index or target_index > len(baseline_lines):
            raise SourceArtifactError("Candidate Patch hunk is outside the Baseline source")
        output.extend(baseline_lines[source_index:target_index])
        source_index = target_index
        index += 1
        consumed_old = 0
        produced_new = 0
        while consumed_old < old_count or produced_new < new_count:
            if index >= len(patch_lines):
                raise SourceArtifactError("Candidate Patch hunk ended before its declared counts")
            line = patch_lines[index]
            if line.startswith("\\ No newline at end of file"):
                raise SourceArtifactError("Candidate Patch newline markers are not supported")
            if line.startswith(" "):
                if source_index >= len(baseline_lines):
                    raise SourceArtifactError("Candidate Patch context exceeds Baseline source")
                expected = line[1:].encode("utf-8")
                if baseline_lines[source_index] != expected:
                    raise SourceArtifactError(
                        "Candidate Patch context differs from Baseline source"
                    )
                output.append(baseline_lines[source_index])
                source_index += 1
                consumed_old += 1
                produced_new += 1
            elif line.startswith("-"):
                if source_index >= len(baseline_lines):
                    raise SourceArtifactError("Candidate Patch deletion exceeds Baseline source")
                expected = line[1:].encode("utf-8")
                if baseline_lines[source_index] != expected:
                    raise SourceArtifactError(
                        "Candidate Patch deletion differs from Baseline source"
                    )
                source_index += 1
                consumed_old += 1
            elif line.startswith("+"):
                output.append(line[1:].encode("utf-8"))
                produced_new += 1
            else:
                raise SourceArtifactError("Candidate Patch hunk contains an unsupported line")
            index += 1
        if consumed_old != old_count or produced_new != new_count:
            raise SourceArtifactError("Candidate Patch hunk counts are inconsistent")
    if hunk_count < 1:
        raise SourceArtifactError("Candidate Patch contains no unified hunk")
    output.extend(baseline_lines[source_index:])
    candidate = b"".join(output)
    if candidate == baseline:
        raise SourceArtifactError("Candidate Patch does not change the Baseline source")
    return candidate


@dataclass(frozen=True, slots=True)
class BaselineOverlaySource:
    snapshot: SourceSnapshot
    path: str


@dataclass(frozen=True, slots=True)
class PreparedCandidatePackage:
    resolved: ResolvedRetainedProposal
    review: CandidateProposalReviewRecord
    candidate_id: UUID
    source_package_ref: CandidateSourcePackageRef


class CandidateSourcePackagePublisher:
    """Publish one approved real Proposal into the existing M1/M2a package format."""

    def __init__(
        self,
        root: Path,
        *,
        profile: str,
        allowed_overlay_roots: tuple[str, ...],
        approved_mount_targets: dict[str, str],
    ) -> None:
        self.root = root.resolve()
        self.allowed_overlay_roots = tuple(value.strip("/") for value in allowed_overlay_roots)
        self.approved_mount_targets = dict(approved_mount_targets)
        self.source_packages = CandidateSourcePackageStore(
            self.root,
            profile=profile,
            allowed_overlay_roots=allowed_overlay_roots,
            approved_mount_targets=approved_mount_targets,
        )

    @staticmethod
    def _read_frozen_baseline(
        baseline: BaselineOverlaySource,
        *,
        expected_source_hash: str,
    ) -> bytes:
        snapshot = baseline.snapshot
        if (
            snapshot.kind != "baseline"
            or not snapshot.clean
            or snapshot.source_hash != expected_source_hash
        ):
            raise SourceArtifactError(
                "Candidate Package Baseline Snapshot does not match Request authority"
            )
        try:
            declared_root = file_uri_to_path(snapshot.worktree_uri)
            if declared_root.is_symlink() or not declared_root.is_dir():
                raise SourceArtifactError(
                    "Candidate Package Baseline Worktree is not a regular directory"
                )
            root = declared_root.resolve(strict=True)
            if canonical_source_hash(root) != snapshot.source_hash:
                raise SourceArtifactError(
                    "Candidate Package Baseline authority drifted before publish"
                )
            source = root / baseline.path
            if source.is_symlink() or not source.is_file():
                raise SourceArtifactError(
                    "Candidate Package Baseline Overlay source is not a regular file"
                )
            resolved_source = source.resolve(strict=True)
            try:
                resolved_source.relative_to(root)
            except ValueError as error:
                raise SourceArtifactError(
                    "Candidate Package Baseline Overlay source escapes its Worktree"
                ) from error
            if source.stat().st_size > MAX_PROPOSAL_PATCH_BYTES:
                raise SourceArtifactError(
                    "Candidate Package Baseline Overlay source exceeds its size limit"
                )
            content = source.read_bytes()
            if canonical_source_hash(root) != snapshot.source_hash:
                raise SourceArtifactError(
                    "Candidate Package Baseline authority drifted while reading source"
                )
            return content
        except SourceArtifactError:
            raise
        except (OSError, SourceIntegrityError) as error:
            raise SourceArtifactError(
                "Candidate Package Baseline Snapshot cannot be independently reread"
            ) from error

    def publish(
        self,
        resolved: ResolvedRetainedProposal,
        review: CandidateProposalReviewRecord,
        *,
        baseline: BaselineOverlaySource,
        candidate_id: UUID,
    ) -> PreparedCandidatePackage:
        request = resolved.status.run.request
        proposal = resolved.proposal
        verify_candidate_proposal_review_record(
            request,
            proposal,
            resolved.raw_patch,
            review,
        )
        if review.decision != "approved":
            raise SourceArtifactError("rejected Candidate Proposal cannot publish a Package")
        if (
            resolved.batch.synthetic
            or resolved.batch.adapter_provenance.implementation_kind == "fake"
        ):
            raise SourceArtifactError(
                "synthetic or fake Proposal cannot publish a business Package"
            )
        if len(proposal.touched_paths) != 1 or proposal.touched_paths[0] != baseline.path:
            raise SourceArtifactError("Candidate promotion requires one approved Overlay path")
        if not any(
            baseline.path == root or baseline.path.startswith(f"{root}/")
            for root in self.allowed_overlay_roots
        ):
            raise SourceArtifactError("Candidate Proposal touches an unapproved Overlay root")
        mount_target = self.approved_mount_targets.get(request.replacement_point)
        if mount_target is None:
            raise SourceArtifactError("Candidate replacement point has no approved mount target")
        baseline_content = self._read_frozen_baseline(
            baseline,
            expected_source_hash=request.baseline_source_hash,
        )
        candidate_content = apply_single_file_unified_patch(
            baseline_content,
            resolved.raw_patch,
            expected_path=baseline.path,
        )

        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".candidate-source-", dir=self.root))
        moved = False
        try:
            source = staging / "files" / baseline.path
            source.parent.mkdir(parents=True)
            source.write_bytes(candidate_content)
            candidate_source_hash = canonical_source_hash(staging / "files")
            manifest = CandidateSourcePackageManifest(
                candidate_id=candidate_id,
                hotspot_id=request.hotspot_id,
                baseline_source_hash=request.baseline_source_hash,
                candidate_source_hash=candidate_source_hash,
                replacement_point=request.replacement_point,
                candidate_kind=ManualCandidateKind.BUSINESS,
                overlay_mount_target=mount_target,
                files=[
                    CandidateOverlayFile(
                        path=baseline.path,
                        content_hash=_sha256(candidate_content),
                    )
                ],
                profiler_evidence_uri=request.profiler_evidence_uri,
                profiler_evidence_hash=request.profiler_evidence_hash,
                reviewed_by=review.reviewer,
                reviewed_at=review.reviewed_at,
            )
            manifest_bytes = canonical_json_bytes(manifest)
            manifest_hash = _sha256(manifest_bytes)
            package_hash = candidate_source_package_hash(manifest_hash, manifest.files)
            (staging / "manifest.json").write_bytes(manifest_bytes)
            digest = candidate_source_hash.removeprefix("sha256:")
            destination = self.root / "sha256" / digest[:2] / digest[2:]
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                loaded = self.source_packages.read(
                    candidate_source_hash=candidate_source_hash
                )
                if loaded.manifest != manifest or loaded.manifest_hash != manifest_hash:
                    raise SourceArtifactError(
                        "Candidate source identity already contains a different Package"
                    )
            else:
                try:
                    os.rename(staging, destination)
                    moved = True
                except OSError as error:
                    if not destination.exists():
                        raise SourceArtifactError(
                            "Candidate Package atomic publication failed"
                        ) from error
                    loaded = self.source_packages.read(
                        candidate_source_hash=candidate_source_hash
                    )
                    if loaded.manifest != manifest or loaded.manifest_hash != manifest_hash:
                        raise SourceArtifactError(
                            "Candidate Package concurrent publication changed immutable bytes"
                        ) from error
            loaded = self.source_packages.read(candidate_source_hash=candidate_source_hash)
            actual_package_hash = candidate_source_package_hash(
                loaded.manifest_hash,
                loaded.manifest.files,
            )
            if loaded.manifest != manifest or actual_package_hash != package_hash:
                raise SourceArtifactError("published Candidate Package failed independent reread")
            reference = CandidateSourcePackageRef(
                candidate_source_hash=candidate_source_hash,
                source_package_hash=package_hash,
                manifest_hash=manifest_hash,
                manifest_schema_version=manifest.schema_version,
            )
            return PreparedCandidatePackage(
                resolved=resolved,
                review=review,
                candidate_id=candidate_id,
                source_package_ref=reference,
            )
        finally:
            if not moved and staging.exists():
                shutil.rmtree(staging)


class ProposalPromotionService:
    def __init__(
        self,
        *,
        review_authority: ProposalReviewAuthority,
        package_publisher: CandidateSourcePackagePublisher,
        decision_store: ProposalDecisionStore,
    ) -> None:
        self.review_authority = review_authority
        self.package_publisher = package_publisher
        self.decision_store = decision_store

    def prepare(
        self,
        status: GenerationRunStatusView,
        proposal_id: UUID,
        review_id: UUID,
        *,
        baseline: BaselineOverlaySource,
        candidate_id: UUID,
    ) -> PreparedCandidatePackage:
        resolved = self.review_authority.resolve(status, proposal_id)
        review = self.decision_store.load_review(review_id)
        return self.package_publisher.publish(
            resolved,
            review,
            baseline=baseline,
            candidate_id=candidate_id,
        )

    def finalize(
        self,
        prepared: PreparedCandidatePackage,
        family_manifest: BusinessCandidateFamilyManifest,
        family_verifier: BusinessCandidateFamilyVerifier,
        *,
        promoted_by: str,
        promoted_at: datetime,
        idempotency_key: str,
    ) -> CandidateProposalPromotionReceipt:
        request = prepared.resolved.status.run.request
        proposal = prepared.resolved.proposal
        member_matches = [
            member
            for member in family_manifest.members
            if member.candidate_id == prepared.candidate_id
            and member.source_package_ref == prepared.source_package_ref
            and member.optimization_intent == proposal.optimization_intent
        ]
        authority = (
            family_manifest.target_snapshot_id,
            family_manifest.stage0_run_id,
            family_manifest.baseline_epoch_id,
            family_manifest.baseline_source_hash,
            family_manifest.hotspot_id,
            family_manifest.replacement_point,
            family_manifest.profiler_evidence_uri,
            family_manifest.profiler_evidence_hash,
        )
        expected_authority = (
            request.target_snapshot_id,
            request.stage0_run_id,
            request.baseline_epoch_id,
            request.baseline_source_hash,
            request.hotspot_id,
            request.replacement_point,
            request.profiler_evidence_uri,
            request.profiler_evidence_hash,
        )
        if len(member_matches) != 1 or authority != expected_authority:
            raise SourceArtifactError(
                "Candidate Family does not bind the approved Proposal authority"
            )
        verified_family = family_verifier.verify(family_manifest)
        promotion_id = uuid5(
            NAMESPACE_URL,
            f"hcuopt:m2b-proposal-promotion:{idempotency_key}",
        )
        evidence_document = {
            "schema_version": "m2b-source-family-verification-evidence-v1",
            "promotion_id": str(promotion_id),
            "proposal_id": str(proposal.proposal_id),
            "candidate_id": str(prepared.candidate_id),
            "source_package_ref": prepared.source_package_ref.model_dump(mode="json"),
            "source_family_hash": verified_family.source_family_hash,
            "family_manifest": family_manifest.model_dump(mode="json"),
            "verifier_provenance": family_verifier.provenance.model_dump(mode="json"),
        }
        evidence = self.decision_store.publish_evidence(
            "source-family-verification",
            canonical_json_bytes(evidence_document),
        )
        review = prepared.review
        receipt = CandidateProposalPromotionReceipt(
            promotion_id=promotion_id,
            idempotency_key=idempotency_key,
            proposal_id=review.proposal_id,
            proposal_hash=review.proposal_hash,
            request_id=review.request_id,
            request_hash=review.request_hash,
            generation_run_id=review.generation_run_id,
            patch_uri=review.patch_uri,
            patch_hash=review.patch_hash,
            normalized_patch_hash=review.normalized_patch_hash,
            baseline_epoch_id=review.baseline_epoch_id,
            baseline_source_hash=review.baseline_source_hash,
            hotspot_id=review.hotspot_id,
            replacement_point=review.replacement_point,
            review=review,
            review_record_hash=candidate_proposal_review_record_hash(review),
            candidate_id=prepared.candidate_id,
            source_package_ref=prepared.source_package_ref,
            source_family_hash=verified_family.source_family_hash,
            source_family_verification_evidence_uri=evidence.uri,
            source_family_verification_evidence_hash=evidence.content_hash,
            source_family_verifier_provenance=family_verifier.provenance,
            promoted_by=promoted_by,
            promoted_at=promoted_at,
            synthetic=False,
        )
        verify_candidate_proposal_promotion_receipt(
            request,
            proposal,
            prepared.resolved.raw_patch,
            receipt,
        )
        stored = self.decision_store.publish_receipt(receipt)
        if candidate_proposal_promotion_receipt_hash(stored) != (
            candidate_proposal_promotion_receipt_hash(receipt)
        ):
            raise SourceArtifactError("Candidate Proposal promotion changed after publication")
        return stored


__all__ = [
    "BaselineOverlaySource",
    "CandidateSourcePackagePublisher",
    "PreparedCandidatePackage",
    "ProposalDecisionStore",
    "ProposalPromotionService",
    "ProposalReviewAuthority",
    "ResolvedRetainedProposal",
    "apply_single_file_unified_patch",
]
