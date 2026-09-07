# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal, Protocol, TypeVar
from uuid import UUID

from pydantic import ConfigDict, Field, ValidationError, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.formal_evidence_acceptance_v1 import (
    FormalEvidenceAcceptanceReview,
    FormalEvidenceAcceptanceReviewContent,
    FormalEvidenceAcceptanceReviewSignature,
    FormalEvidenceAuthoritySet,
    FormalEvidenceVerifierIdentity,
    ProductionEvidenceRootDescriptor,
    ProductionFormalEvidenceRef,
    formal_evidence_acceptance_review_hash,
    publish_formal_evidence_acceptance_review,
)
from hcuopt.contracts.m2 import SearchRound
from hcuopt.contracts.m2_formal_authority_v1 import (
    FormalAuthorityContextDescriptor,
    formal_authority_context_ref,
)
from hcuopt.contracts.m2_formal_signoff_v1 import (
    FormalRoundSignoffArtifactPublication,
    FormalRoundSignoffDecisionArtifact,
    FormalRoundSignoffIntent,
)
from hcuopt.contracts.platform_v1 import GIT_COMMIT_PATTERN, SHA256_PATTERN
from hcuopt.domain.enums import RoundPhase, RoundTerminalReason
from hcuopt.evaluation.m2_formal_authority import (
    FormalHoldoutRevealPersistence,
    formal_authority_payload_hash,
)
from hcuopt.evaluation.m2_formal_finalizer import M2FormalRoundFinalizationService
from hcuopt.evaluation.m2_formal_signoff import FormalRoundSignoffFinalizationService
from hcuopt.evaluation.m2_models import (
    MultipleComparisonResult,
    RoundBarrierResult,
    RoundEvidenceBundle,
)
from hcuopt.evaluation.production_evidence import VerifiedProductionEvidence
from hcuopt.measurement.evidence import EvidenceArtifact, canonical_json_bytes

FORMAL_EVIDENCE_ACCEPTANCE_SNAPSHOT_VERSION = "m2a-formal-acceptance-snapshot-v1"
FORMAL_EVIDENCE_VERIFICATION_SUMMARY_VERSION = "m2a-formal-verification-summary-v1"


class FormalEvidenceAcceptanceError(RuntimeError):
    """A D-owned acceptance record could not be issued safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _FrozenAcceptanceModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class FormalEvidenceAcceptanceSnapshot(_FrozenAcceptanceModel):
    """Deployment-owned identities used to locate one terminal Formal Round."""

    schema_version: Literal["m2a-formal-acceptance-snapshot-v1"] = (
        FORMAL_EVIDENCE_ACCEPTANCE_SNAPSHOT_VERSION
    )
    round_id: UUID
    readiness_audit_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    readiness_audit_base_commit: str = Field(pattern=GIT_COMMIT_PATTERN)
    readiness_manifest_hash: str = Field(pattern=SHA256_PATTERN)
    readiness_report_hash: str = Field(pattern=SHA256_PATTERN)
    target_lock_hash: str = Field(pattern=SHA256_PATTERN)
    terminal_path: Literal["zero_promotion", "holdout_fwer"]
    authority_context_id: UUID
    authority_context_hash: str = Field(pattern=SHA256_PATTERN)
    round_evidence_bundle_id: UUID
    round_evidence_bundle_hash: str = Field(pattern=SHA256_PATTERN)
    signoff_artifact_hash: str = Field(pattern=SHA256_PATTERN)
    signoff_signer_identity_hash: str = Field(pattern=SHA256_PATTERN)
    evidence_root: ProductionEvidenceRootDescriptor
    verifier: FormalEvidenceVerifierIdentity
    authorities: FormalEvidenceAuthoritySet
    references: tuple[ProductionFormalEvidenceRef, ...] = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def require_frozen_authorities(self) -> FormalEvidenceAcceptanceSnapshot:
        if self.authorities.independent_verifier != self.verifier:
            raise ValueError("Formal acceptance snapshot changed the D Verifier identity")
        return self


class FormalEvidenceVerificationSummary(_FrozenAcceptanceModel):
    schema_version: Literal["m2a-formal-verification-summary-v1"] = (
        FORMAL_EVIDENCE_VERIFICATION_SUMMARY_VERSION
    )
    round_id: UUID
    readiness_audit_id: str
    authority_context_id: UUID
    authority_context_hash: str = Field(pattern=SHA256_PATTERN)
    terminal_path: Literal["zero_promotion", "holdout_fwer"]
    verification_input_digest: str = Field(pattern=SHA256_PATTERN)
    object_verification_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    verified_evidence_count: int = Field(ge=0)
    recursive_semantic_verification: Literal["verified", "blocked"]
    allowlisted_signature_verification: Literal["verified", "blocked"]
    decision: Literal["accepted_for_formal_window", "blocked"]
    blocker_codes: tuple[str, ...] = Field(default=(), max_length=64)
    signoff_artifact_hash: str = Field(pattern=SHA256_PATTERN)
    signoff_signer_identity_hash: str = Field(pattern=SHA256_PATTERN)
    reviewed_at: datetime
    owner_window_authorization: Literal["not_granted"] = "not_granted"
    hcu_accessed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_consistent_result(self) -> FormalEvidenceVerificationSummary:
        if self.reviewed_at.tzinfo is None or self.reviewed_at.utcoffset() is None:
            raise ValueError("Formal verification summary time must be timezone-aware")
        if self.blocker_codes != tuple(sorted(set(self.blocker_codes))):
            raise ValueError("Formal verification summary blockers must be unique and sorted")
        accepted = self.decision == "accepted_for_formal_window"
        verified = (
            self.recursive_semantic_verification == "verified"
            and self.allowlisted_signature_verification == "verified"
            and self.verified_evidence_count > 0
        )
        if accepted != (verified and not self.blocker_codes):
            raise ValueError("Formal verification summary decision disagrees with verification")
        return self


class FormalEvidenceAcceptanceSnapshotReader(Protocol):
    def read(
        self, *, round_id: UUID, readiness_audit_id: str
    ) -> FormalEvidenceAcceptanceSnapshot: ...


class ProductionEvidenceVerificationService(Protocol):
    root: ProductionEvidenceRootDescriptor
    verifier: FormalEvidenceVerifierIdentity

    def read_raw_bytes(self, uri: str, expected_hash: str) -> bytes: ...

    def verify(
        self,
        *,
        context: FormalAuthorityContextDescriptor,
        authorities: FormalEvidenceAuthoritySet,
        references: tuple[ProductionFormalEvidenceRef, ...],
    ) -> VerifiedProductionEvidence: ...


class FormalEvidenceVerificationSummaryPublisher(Protocol):
    def publish(self, summary: FormalEvidenceVerificationSummary) -> EvidenceArtifact: ...


class FormalEvidenceAcceptanceReviewSigner(Protocol):
    def sign(self, payload: bytes) -> FormalEvidenceAcceptanceReviewSignature: ...


class FormalEvidenceAcceptanceReviewSignatureVerifier(Protocol):
    verifier_id: str
    verifier_key_id: str
    verifier_identity_hash: str
    algorithm: str

    def verify(
        self, payload: bytes, signature: FormalEvidenceAcceptanceReviewSignature
    ) -> bool: ...


_ModelT = TypeVar("_ModelT", bound=ContractModel)


class FormalEvidenceAcceptanceService:
    """Reread, rebuild, authenticate, and sign one Formal terminal path."""

    def __init__(
        self,
        *,
        snapshot_reader: FormalEvidenceAcceptanceSnapshotReader,
        object_verifier: ProductionEvidenceVerificationService,
        round_finalizer: M2FormalRoundFinalizationService,
        signoff_finalizer: FormalRoundSignoffFinalizationService,
        summary_publisher: FormalEvidenceVerificationSummaryPublisher,
        review_signer: FormalEvidenceAcceptanceReviewSigner,
        review_signature_verifier: FormalEvidenceAcceptanceReviewSignatureVerifier,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.snapshot_reader = snapshot_reader
        self.object_verifier = object_verifier
        self.round_finalizer = round_finalizer
        self.signoff_finalizer = signoff_finalizer
        self.summary_publisher = summary_publisher
        self.review_signer = review_signer
        self.review_signature_verifier = review_signature_verifier
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def review(
        self, *, round_id: UUID, readiness_audit_id: str
    ) -> FormalEvidenceAcceptanceReview:
        try:
            snapshot = FormalEvidenceAcceptanceSnapshot.model_validate(
                self.snapshot_reader.read(
                    round_id=round_id, readiness_audit_id=readiness_audit_id
                )
            )
        except Exception as error:
            raise FormalEvidenceAcceptanceError(
                "formal_acceptance_snapshot_unavailable",
                "Formal acceptance snapshot cannot be loaded",
            ) from error
        self._verify_snapshot_identity(snapshot, round_id, readiness_audit_id)

        input_digest = _hash_bytes(canonical_json_bytes(snapshot))
        object_digest = None
        verified_count = 0
        semantics = "blocked"
        signatures = "blocked"
        blocker_codes: tuple[str, ...] = ()
        try:
            context = self._read_single(
                snapshot, "authority_context", FormalAuthorityContextDescriptor
            )
            self._verify_context_identity(snapshot, context)
            verified = self.object_verifier.verify(
                context=context,
                authorities=snapshot.authorities,
                references=snapshot.references,
            )
            expected_objects = tuple(
                sorted(
                    snapshot.references,
                    key=lambda item: (item.evidence_class, item.sha256, item.uri),
                )
            )
            if (
                verified.authority_context_hash != context.context_hash
                or verified.evidence_root_hash != snapshot.evidence_root.root_hash
                or verified.verifier_identity_hash != snapshot.verifier.identity_hash
                or verified.verified_objects != expected_objects
            ):
                raise FormalEvidenceAcceptanceError(
                    "formal_object_verification_result_mismatch",
                    "Object verifier returned evidence for another Formal input",
                )
            self._verify_context_evidence_refs(snapshot, context)
            object_digest = verified.input_digest
            verified_count = len(verified.verified_objects)
            terminal = self._read_terminal_path(snapshot, context)
            self.round_finalizer.verify(
                context=context,
                round_authority=terminal.round_authority,
                search_barrier=terminal.search_barrier,
                holdout_reveal=terminal.holdout_reveal,
                holdout_barrier=terminal.holdout_barrier,
                multiple_comparison=terminal.multiple_comparison,
                evidence_bundle=terminal.evidence_bundle,
            )
            semantics = "verified"
            artifact = self.signoff_finalizer.verify(
                intent=terminal.signoff_intent,
                publication=terminal.signoff_publication,
            )
            self._verify_signoff_binding(snapshot, context, terminal, artifact)
            signatures = "verified"
            if artifact.content.decision != "approved":
                raise FormalEvidenceAcceptanceError(
                    "formal_signoff_rejected",
                    "Project owner rejected the Formal Round evidence",
                )
        except Exception as error:
            blocker_codes = (_error_code(error),)

        accepted = not blocker_codes
        decision = "accepted_for_formal_window" if accepted else "blocked"
        reviewed_at = self.clock()
        if reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
            raise FormalEvidenceAcceptanceError(
                "formal_acceptance_clock_invalid", "Formal acceptance clock must be timezone-aware"
            )
        summary = FormalEvidenceVerificationSummary(
            round_id=snapshot.round_id,
            readiness_audit_id=snapshot.readiness_audit_id,
            authority_context_id=snapshot.authority_context_id,
            authority_context_hash=snapshot.authority_context_hash,
            terminal_path=snapshot.terminal_path,
            verification_input_digest=input_digest,
            object_verification_digest=object_digest,
            verified_evidence_count=verified_count,
            recursive_semantic_verification=semantics,
            allowlisted_signature_verification=signatures,
            decision=decision,
            blocker_codes=blocker_codes,
            signoff_artifact_hash=snapshot.signoff_artifact_hash,
            signoff_signer_identity_hash=snapshot.signoff_signer_identity_hash,
            reviewed_at=reviewed_at,
        )
        summary_artifact = self._publish_and_verify_summary(summary)
        content = FormalEvidenceAcceptanceReviewContent(
            review_id=f"m2a-d-review-{summary_artifact.sha256.removeprefix('sha256:')}",
            decision=decision,
            blocker_codes=blocker_codes,
            reason=(
                "Recursive Formal evidence and allowlisted Signoff verification passed."
                if accepted
                else f"Formal evidence verification blocked: {blocker_codes[0]}."
            ),
            readiness_audit_id=snapshot.readiness_audit_id,
            readiness_audit_base_commit=snapshot.readiness_audit_base_commit,
            readiness_manifest_hash=snapshot.readiness_manifest_hash,
            readiness_report_hash=snapshot.readiness_report_hash,
            round_id=snapshot.round_id,
            authority_context_id=snapshot.authority_context_id,
            authority_context_hash=snapshot.authority_context_hash,
            target_lock_hash=snapshot.target_lock_hash,
            terminal_path=snapshot.terminal_path,
            round_evidence_bundle_id=snapshot.round_evidence_bundle_id,
            round_evidence_bundle_hash=snapshot.round_evidence_bundle_hash,
            signoff_artifact_hash=snapshot.signoff_artifact_hash,
            signoff_signer_identity_hash=snapshot.signoff_signer_identity_hash,
            evidence_root=snapshot.evidence_root,
            verifier=snapshot.verifier,
            verification_input_digest=input_digest,
            verified_evidence_count=verified_count,
            recursive_semantic_verification=semantics,
            allowlisted_signature_verification=signatures,
            verification_summary_uri=summary_artifact.uri,
            verification_summary_hash=summary_artifact.sha256,
            reviewed_at=reviewed_at,
        )
        try:
            signature = self.review_signer.sign(canonical_json_bytes(content))
            review = publish_formal_evidence_acceptance_review(content, signature=signature)
            verify_formal_evidence_acceptance_review_signature(
                review, self.review_signature_verifier
            )
        except Exception as error:
            raise FormalEvidenceAcceptanceError(
                "formal_acceptance_review_signature_invalid",
                "D acceptance review signature could not be verified",
            ) from error
        return review

    def _verify_snapshot_identity(
        self,
        snapshot: FormalEvidenceAcceptanceSnapshot,
        round_id: UUID,
        readiness_audit_id: str,
    ) -> None:
        if snapshot.round_id != round_id or snapshot.readiness_audit_id != readiness_audit_id:
            raise FormalEvidenceAcceptanceError(
                "formal_acceptance_snapshot_identity_mismatch",
                "Formal acceptance snapshot belongs to another Round or audit",
            )
        if (
            snapshot.evidence_root != self.object_verifier.root
            or snapshot.verifier != self.object_verifier.verifier
        ):
            raise FormalEvidenceAcceptanceError(
                "formal_acceptance_verifier_configuration_drift",
                "Formal acceptance snapshot differs from the deployed Root or Verifier",
            )

    def _verify_context_identity(
        self,
        snapshot: FormalEvidenceAcceptanceSnapshot,
        context: FormalAuthorityContextDescriptor,
    ) -> None:
        if (
            context.round_id != snapshot.round_id
            or context.authority_context_id != snapshot.authority_context_id
            or context.context_hash != snapshot.authority_context_hash
        ):
            raise FormalEvidenceAcceptanceError(
                "formal_acceptance_context_identity_mismatch",
                "Formal Authority Context belongs to another acceptance snapshot",
            )

    def _verify_context_evidence_refs(
        self,
        snapshot: FormalEvidenceAcceptanceSnapshot,
        context: FormalAuthorityContextDescriptor,
    ) -> None:
        expected_hashes = {
            "search_plan": context.search_plan_hash,
            "source_family": context.candidate_family_hash,
            "artifact_family": context.artifact_family_hash,
        }
        for evidence_class, expected_hash in expected_hashes.items():
            reference = self._single_reference(snapshot, evidence_class)
            if reference.sha256 != expected_hash:
                raise FormalEvidenceAcceptanceError(
                    f"formal_{evidence_class}_binding_mismatch",
                    f"Formal {evidence_class} evidence differs from the Authority Context",
                )

    def _read_terminal_path(
        self,
        snapshot: FormalEvidenceAcceptanceSnapshot,
        context: FormalAuthorityContextDescriptor,
    ) -> _FormalTerminalPath:
        round_authority = self._read_single(snapshot, "round_authority", SearchRound)
        evidence_bundle = self._read_single(snapshot, "evidence_bundle", RoundEvidenceBundle)
        signoff_artifact, signoff_reference = self._read_single_with_ref(
            snapshot, "signoff", FormalRoundSignoffDecisionArtifact
        )
        barrier_refs = self._references(snapshot, "barrier")
        barriers = tuple(
            self._read_model(reference, RoundBarrierResult, "barrier")
            for reference in barrier_refs
        )
        by_phase = {barrier.phase: barrier for barrier in barriers}
        if len(by_phase) != len(barriers) or RoundPhase.SEARCH not in by_phase:
            raise FormalEvidenceAcceptanceError(
                "formal_barrier_evidence_incomplete",
                "Formal acceptance requires one unique Search Barrier",
            )

        zero_promotion = (
            evidence_bundle.terminal_reason is RoundTerminalReason.NO_PROMOTABLE_CANDIDATE
        )
        expected_path = "zero_promotion" if zero_promotion else "holdout_fwer"
        if snapshot.terminal_path != expected_path:
            raise FormalEvidenceAcceptanceError(
                "formal_terminal_path_mismatch",
                "Formal terminal path differs from the EvidenceBundle",
            )
        holdout_reveal = None
        holdout_barrier = None
        multiple_comparison = None
        if zero_promotion:
            if (
                len(barriers) != 1
                or self._references(snapshot, "holdout_reveal")
                or self._references(snapshot, "fwer")
            ):
                raise FormalEvidenceAcceptanceError(
                    "formal_zero_promotion_path_invalid",
                    "zero-promotion acceptance contains Holdout or FWER evidence",
                )
        else:
            if len(barriers) != 2 or RoundPhase.HOLDOUT not in by_phase:
                raise FormalEvidenceAcceptanceError(
                    "formal_holdout_evidence_incomplete",
                    "Holdout acceptance requires Search and Holdout Barriers",
                )
            holdout_reveal = self._read_single(
                snapshot, "holdout_reveal", FormalHoldoutRevealPersistence
            )
            holdout_barrier = by_phase[RoundPhase.HOLDOUT]
            multiple_comparison = self._read_single(
                snapshot, "fwer", MultipleComparisonResult
            )

        bundle_reference = self._single_reference(snapshot, "evidence_bundle")
        if (
            bundle_reference.sha256 != snapshot.round_evidence_bundle_hash
            or formal_authority_payload_hash(evidence_bundle) != bundle_reference.sha256
            or evidence_bundle.round_evidence_bundle_id != snapshot.round_evidence_bundle_id
        ):
            raise FormalEvidenceAcceptanceError(
                "formal_evidence_bundle_identity_mismatch",
                "Formal EvidenceBundle differs from the acceptance snapshot",
            )
        signoff_intent = _signoff_intent(signoff_artifact, context)
        signoff_publication = FormalRoundSignoffArtifactPublication(
            signoff_intent_id=signoff_artifact.content.signoff_intent_id,
            round_signoff_id=signoff_artifact.content.round_signoff_id,
            decision_artifact_uri=signoff_reference.uri,
            decision_artifact_hash=signoff_reference.sha256,
            signature=signoff_artifact.signature,
        )
        return _FormalTerminalPath(
            round_authority=round_authority,
            search_barrier=by_phase[RoundPhase.SEARCH],
            holdout_reveal=holdout_reveal,
            holdout_barrier=holdout_barrier,
            multiple_comparison=multiple_comparison,
            evidence_bundle=evidence_bundle,
            evidence_bundle_hash=bundle_reference.sha256,
            signoff_intent=signoff_intent,
            signoff_publication=signoff_publication,
        )

    def _verify_signoff_binding(
        self,
        snapshot: FormalEvidenceAcceptanceSnapshot,
        context: FormalAuthorityContextDescriptor,
        terminal: _FormalTerminalPath,
        artifact: FormalRoundSignoffDecisionArtifact,
    ) -> None:
        content = artifact.content
        owner = snapshot.authorities.project_owner
        if (
            content.round_id != snapshot.round_id
            or content.task_id != context.task_id
            or content.authority_context_id != context.authority_context_id
            or content.authority_context_hash != context.context_hash
            or content.round_evidence_bundle_id != terminal.evidence_bundle.round_evidence_bundle_id
            or content.evidence_bundle_hash != terminal.evidence_bundle_hash
            or content.candidate_family_hash != context.candidate_family_hash
            or content.artifact_family_hash != context.artifact_family_hash
            or content.holdout_family_hash != terminal.evidence_bundle.holdout_family_hash
            or terminal.signoff_publication.decision_artifact_hash
            != snapshot.signoff_artifact_hash
            or artifact.signature.signer_id != owner.producer_id
            or artifact.signature.signer_identity_hash != owner.producer_hash
            or artifact.signature.signer_identity_hash
            != snapshot.signoff_signer_identity_hash
        ):
            raise FormalEvidenceAcceptanceError(
                "formal_signoff_authority_mismatch",
                "Formal Signoff changed Round, Bundle, Family, or project-owner authority",
            )

    def _publish_and_verify_summary(
        self, summary: FormalEvidenceVerificationSummary
    ) -> EvidenceArtifact:
        encoded = canonical_json_bytes(summary)
        try:
            artifact = self.summary_publisher.publish(summary)
            reread = self.object_verifier.read_raw_bytes(artifact.uri, artifact.sha256)
        except Exception as error:
            raise FormalEvidenceAcceptanceError(
                "formal_verification_summary_publish_failed",
                "Formal verification summary was not published into the protected Root",
            ) from error
        if (
            artifact.sha256 != _hash_bytes(encoded)
            or artifact.byte_count != len(encoded)
            or reread != encoded
        ):
            raise FormalEvidenceAcceptanceError(
                "formal_verification_summary_identity_mismatch",
                "Formal verification summary changed during publication",
            )
        return artifact

    def _read_single(
        self,
        snapshot: FormalEvidenceAcceptanceSnapshot,
        evidence_class: str,
        model: type[_ModelT],
    ) -> _ModelT:
        value, _ = self._read_single_with_ref(snapshot, evidence_class, model)
        return value

    def _read_single_with_ref(
        self,
        snapshot: FormalEvidenceAcceptanceSnapshot,
        evidence_class: str,
        model: type[_ModelT],
    ) -> tuple[_ModelT, ProductionFormalEvidenceRef]:
        reference = self._single_reference(snapshot, evidence_class)
        return self._read_model(reference, model, evidence_class), reference

    def _read_model(
        self,
        reference: ProductionFormalEvidenceRef,
        model: type[_ModelT],
        evidence_class: str,
    ) -> _ModelT:
        try:
            encoded = self.object_verifier.read_raw_bytes(reference.uri, reference.sha256)
            value = model.model_validate_json(encoded)
        except Exception as error:
            if isinstance(error, FormalEvidenceAcceptanceError):
                raise
            raise FormalEvidenceAcceptanceError(
                getattr(error, "code", f"formal_{evidence_class}_evidence_invalid"),
                f"Formal {evidence_class} evidence cannot be reread and parsed",
            ) from error
        if canonical_json_bytes(value) != encoded:
            raise FormalEvidenceAcceptanceError(
                f"formal_{evidence_class}_evidence_noncanonical",
                f"Formal {evidence_class} evidence must be canonical JSON",
            )
        return value

    @staticmethod
    def _references(
        snapshot: FormalEvidenceAcceptanceSnapshot, evidence_class: str
    ) -> tuple[ProductionFormalEvidenceRef, ...]:
        return tuple(
            reference
            for reference in snapshot.references
            if reference.evidence_class == evidence_class
        )

    def _single_reference(
        self, snapshot: FormalEvidenceAcceptanceSnapshot, evidence_class: str
    ) -> ProductionFormalEvidenceRef:
        references = self._references(snapshot, evidence_class)
        if len(references) != 1:
            raise FormalEvidenceAcceptanceError(
                f"formal_{evidence_class}_evidence_incomplete",
                f"Formal acceptance requires exactly one {evidence_class} object",
            )
        return references[0]


class _FormalTerminalPath(_FrozenAcceptanceModel):
    round_authority: SearchRound
    search_barrier: RoundBarrierResult
    holdout_reveal: FormalHoldoutRevealPersistence | None
    holdout_barrier: RoundBarrierResult | None
    multiple_comparison: MultipleComparisonResult | None
    evidence_bundle: RoundEvidenceBundle
    evidence_bundle_hash: str = Field(pattern=SHA256_PATTERN)
    signoff_intent: FormalRoundSignoffIntent
    signoff_publication: FormalRoundSignoffArtifactPublication


def verify_formal_evidence_acceptance_review_signature(
    review: FormalEvidenceAcceptanceReview,
    verifier: FormalEvidenceAcceptanceReviewSignatureVerifier,
) -> None:
    signature = review.signature
    expected_identity = (
        review.verifier.verifier_id,
        review.verifier.attestation_key_id,
        review.verifier.identity_hash,
        review.verifier.attestation_scheme,
    )
    deployed_identity = (
        verifier.verifier_id,
        verifier.verifier_key_id,
        verifier.verifier_identity_hash,
        verifier.algorithm,
    )
    signature_identity = (
        signature.verifier_id,
        signature.verifier_key_id,
        signature.verifier_identity_hash,
        signature.algorithm,
    )
    if deployed_identity != expected_identity or signature_identity != expected_identity:
        raise FormalEvidenceAcceptanceError(
            "formal_acceptance_review_signer_not_allowlisted",
            "D review signature identity is not the deployed allowlisted Verifier",
        )
    content = FormalEvidenceAcceptanceReviewContent.model_validate(
        review.model_dump(mode="json", exclude={"review_hash", "signature"})
    )
    if formal_evidence_acceptance_review_hash(content) != review.review_hash:
        raise FormalEvidenceAcceptanceError(
            "formal_acceptance_review_hash_mismatch",
            "D review content changed after signing",
        )
    try:
        verified = verifier.verify(canonical_json_bytes(content), signature)
    except Exception as error:
        raise FormalEvidenceAcceptanceError(
            "formal_acceptance_review_signature_invalid",
            "D review signature is invalid",
        ) from error
    if verified is not True:
        raise FormalEvidenceAcceptanceError(
            "formal_acceptance_review_signature_invalid",
            "D review signature verifier did not affirm the signature",
        )


def _signoff_intent(
    artifact: FormalRoundSignoffDecisionArtifact,
    context: FormalAuthorityContextDescriptor,
) -> FormalRoundSignoffIntent:
    content = artifact.content
    try:
        return FormalRoundSignoffIntent(
            signoff_intent_id=content.signoff_intent_id,
            round_signoff_id=content.round_signoff_id,
            round_id=content.round_id,
            task_id=content.task_id,
            authority_context=formal_authority_context_ref(context),
            round_evidence_bundle_id=content.round_evidence_bundle_id,
            evidence_bundle_hash=content.evidence_bundle_hash,
            candidate_family_hash=content.candidate_family_hash,
            artifact_family_hash=content.artifact_family_hash,
            holdout_family_hash=content.holdout_family_hash,
            decision=content.decision,
            actor=content.actor,
            actor_identity_hash=content.actor_identity_hash,
            reason=content.reason,
            decision_at=content.decision_at,
            input_digest=content.input_digest,
            idempotency_key=content.idempotency_key,
            state="artifact_published",
        )
    except ValidationError as error:
        raise FormalEvidenceAcceptanceError(
            "formal_signoff_intent_invalid",
            "Formal Signoff Artifact cannot reconstruct its immutable Intent",
        ) from error


def _error_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    return "formal_evidence_verification_failed"


def _hash_bytes(encoded: bytes) -> str:
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
