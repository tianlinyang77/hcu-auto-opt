# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

import pytest

from hcuopt.contracts.formal_evidence_acceptance_v1 import (
    FormalEvidenceAcceptanceReviewSignature,
    FormalEvidenceAuthoritySet,
    FormalEvidenceVerifierIdentityContent,
    FormalProducerIdentity,
    ProductionEvidenceRootContent,
    ProductionFormalEvidenceRef,
    publish_formal_evidence_verifier_identity,
    publish_production_evidence_root,
)
from hcuopt.contracts.m2_formal_authority_v1 import (
    FormalAuthorityContextContent,
    FormalEvidenceStoreRef,
    FormalVerifierRef,
    formal_authority_context_ref,
    publish_formal_authority_context,
)
from hcuopt.contracts.m2_formal_signoff_v1 import (
    FormalDecisionSignature,
    FormalRoundSignoffDecisionArtifact,
    FormalRoundSignoffRequest,
    build_formal_round_signoff_intent,
    formal_round_signoff_artifact_content,
)
from hcuopt.evaluation.formal_evidence_acceptance import (
    FormalEvidenceAcceptanceError,
    FormalEvidenceAcceptanceService,
    FormalEvidenceAcceptanceSnapshot,
    FormalEvidenceVerificationSummary,
    verify_formal_evidence_acceptance_review_signature,
)
from hcuopt.evaluation.m2_formal_finalizer import (
    M2FormalRoundFinalizer,
    build_formal_round_evidence,
    formal_round_evidence_requirements,
)
from hcuopt.evaluation.m2_formal_signoff import M2FormalRoundSignoffFinalizer
from hcuopt.evaluation.production_evidence import VerifiedProductionEvidence
from hcuopt.measurement.evidence import EvidenceArtifact, canonical_json_bytes
from tests.unit.test_m2_formal_finalizer import (
    NOW,
    _fixture,
    _FormalStore,
    _holdout_fixture,
    _publish_index,
)

OWNER_KEY = b"formal-owner-signoff-test-key"
REVIEW_KEY = b"formal-d-review-test-key"


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _producer(role: str, character: str, capability: str) -> FormalProducerIdentity:
    return FormalProducerIdentity(
        producer_role=role,
        producer_id=f"{role}-authority",
        producer_hash=_hash(character),
        capability=capability,
    )


class _OwnerSigner:
    def __init__(self, owner: FormalProducerIdentity, *, wrong_identity: bool = False) -> None:
        self.owner = owner
        self.wrong_identity = wrong_identity

    def sign(self, payload: bytes) -> FormalDecisionSignature:
        return FormalDecisionSignature(
            signer_id=self.owner.producer_id,
            signer_key_id="project-owner-key-v1",
            signer_identity_hash=(
                _hash("0") if self.wrong_identity else self.owner.producer_hash
            ),
            algorithm="test-sha256-v1",
            value=hashlib.sha256(OWNER_KEY + payload).hexdigest(),
        )


class _OwnerVerifier:
    def verify(self, payload: bytes, signature: FormalDecisionSignature) -> None:
        if signature.value != hashlib.sha256(OWNER_KEY + payload).hexdigest():
            raise ValueError("owner signature rejected")


class _ReviewSigner:
    def __init__(self, verifier) -> None:  # type: ignore[no-untyped-def]
        self.verifier = verifier

    def sign(self, payload: bytes) -> FormalEvidenceAcceptanceReviewSignature:
        return FormalEvidenceAcceptanceReviewSignature(
            verifier_id=self.verifier.verifier_id,
            verifier_key_id=self.verifier.attestation_key_id,
            verifier_identity_hash=self.verifier.identity_hash,
            algorithm=self.verifier.attestation_scheme,
            value=hashlib.sha256(REVIEW_KEY + payload).hexdigest(),
        )


class _ReviewVerifier:
    def __init__(self, verifier) -> None:  # type: ignore[no-untyped-def]
        self.verifier_id = verifier.verifier_id
        self.verifier_key_id = verifier.attestation_key_id
        self.verifier_identity_hash = verifier.identity_hash
        self.algorithm = verifier.attestation_scheme

    def verify(
        self, payload: bytes, signature: FormalEvidenceAcceptanceReviewSignature
    ) -> bool:
        if signature.value != hashlib.sha256(REVIEW_KEY + payload).hexdigest():
            raise ValueError("D review signature rejected")
        return True


class _SummaryPublisher:
    def __init__(self, store: _FormalStore) -> None:
        self.store = store

    def publish(self, summary: FormalEvidenceVerificationSummary) -> EvidenceArtifact:
        return self.store.publish(summary)


class _ObjectVerifier:
    def __init__(self, store: _FormalStore, root, verifier) -> None:  # type: ignore[no-untyped-def]
        self.store = store
        self.root = root
        self.verifier = verifier

    def read_raw_bytes(self, uri: str, expected_hash: str) -> bytes:
        return self.store.read_raw_bytes(uri, expected_hash)

    def verify(self, *, context, authorities, references) -> VerifiedProductionEvidence:  # type: ignore[no-untyped-def]
        if context.evidence_store.store_hash != self.root.root_hash:
            raise ValueError("test Root binding changed")
        self.read_raw_bytes(
            self.verifier.identity_evidence_uri,
            self.verifier.identity_evidence_hash,
        )
        ordered = tuple(
            sorted(references, key=lambda item: (item.evidence_class, item.sha256, item.uri))
        )
        for reference in ordered:
            encoded = self.read_raw_bytes(reference.uri, reference.sha256)
            if len(encoded) != reference.byte_count:
                raise ValueError("test object byte count changed")
        payload = {
            "context": context.context_hash,
            "authorities": authorities.model_dump(mode="json"),
            "references": [item.model_dump(mode="json") for item in ordered],
        }
        return VerifiedProductionEvidence(
            authority_context_hash=context.context_hash,
            evidence_root_hash=self.root.root_hash,
            verifier_identity_hash=self.verifier.identity_hash,
            verified_objects=ordered,
            input_digest="sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
        )


@dataclass
class _SnapshotReader:
    snapshot: FormalEvidenceAcceptanceSnapshot

    def read(
        self, *, round_id: UUID, readiness_audit_id: str
    ) -> FormalEvidenceAcceptanceSnapshot:
        return self.snapshot


@dataclass
class _AcceptanceFixture:
    service: FormalEvidenceAcceptanceService
    snapshot: FormalEvidenceAcceptanceSnapshot
    reader: _SnapshotReader
    store: _FormalStore
    bundle_ref: ProductionFormalEvidenceRef


def _ref(
    *,
    evidence_class: str,
    artifact: EvidenceArtifact,
    producer_role: str,
    producer_id: str,
    producer_hash: str,
) -> ProductionFormalEvidenceRef:
    return ProductionFormalEvidenceRef(
        evidence_class=evidence_class,
        uri=artifact.uri,
        sha256=artifact.sha256,
        byte_count=artifact.byte_count,
        producer_role=producer_role,
        producer_id=producer_id,
        producer_hash=producer_hash,
        created_at=NOW,
    )


def _acceptance_fixture(
    *, holdout: bool = False, wrong_owner_identity: bool = False
) -> _AcceptanceFixture:
    if holdout:
        (
            store,
            old_context,
            round_authority,
            search_barrier,
            reveal,
            holdout_barrier,
            comparison,
            budget_hash,
        ) = _holdout_fixture()
    else:
        store, old_context, round_authority, search_barrier, budget_hash = _fixture()
        reveal = None
        holdout_barrier = None
        comparison = None

    identity_artifact = store.publish({"identity": "formal-d-verifier"})
    verifier = publish_formal_evidence_verifier_identity(
        FormalEvidenceVerifierIdentityContent(
            verifier_id=old_context.verifier.verifier_id,
            verifier_version=old_context.verifier.verifier_version,
            executable_hash=_hash("1"),
            configuration_hash=_hash("2"),
            source_commit="1" * 40,
            attestation_scheme="test-sha256-v1",
            attestation_key_id="deployment/d/formal-review-key-v1",
            identity_evidence_uri=identity_artifact.uri,
            identity_evidence_hash=identity_artifact.sha256,
        )
    )
    root = publish_production_evidence_root(
        ProductionEvidenceRootContent(
            root_id="formal-evidence-v1",
            root_version=1,
            root_uri="file:///protected/m2",
            access_policy_hash=_hash("3"),
            retention_policy_hash=_hash("4"),
            deployment_owner="formal-evidence-test-service",
            max_object_bytes=1024 * 1024,
        )
    )
    context_content = FormalAuthorityContextContent.model_validate(
        {
            **old_context.model_dump(mode="json", exclude={"context_hash"}),
            "evidence_store": FormalEvidenceStoreRef(
                store_id=root.root_id,
                store_version=root.root_version,
                store_hash=root.root_hash,
                access_policy_hash=root.access_policy_hash,
            ),
            "verifier": FormalVerifierRef(
                verifier_id=verifier.verifier_id,
                verifier_version=verifier.verifier_version,
                verifier_hash=verifier.identity_hash,
            ),
        }
    )
    context = publish_formal_authority_context(context_content)
    if reveal is not None:
        reveal = reveal.model_copy(update={"context": formal_authority_context_ref(context)})

    requirements = formal_round_evidence_requirements(
        context=context,
        round_authority=round_authority,
        search_barrier=search_barrier,
        budget_ledger_hash=budget_hash,
        holdout_reveal=reveal,
        holdout_barrier=holdout_barrier,
        multiple_comparison=comparison,
    )
    index = _publish_index(store, context, requirements)
    bundle = build_formal_round_evidence(
        context=context,
        round_authority=round_authority,
        search_barrier=search_barrier,
        budget_ledger_hash=budget_hash,
        evidence_index_uri=index.uri,
        evidence_index_hash=index.sha256,
        evidence_reader=store,
        holdout_reveal=reveal,
        holdout_barrier=holdout_barrier,
        multiple_comparison=comparison,
    )

    control_plane = _producer("control_plane", "5", "formal_control_plane")
    measurement = _producer("measurement_producer", "6", "formal_measurement")
    source = _producer("source_artifact_producer", "7", "formal_source_artifact")
    owner = _producer("project_owner", "8", "formal_signoff")
    authorities = FormalEvidenceAuthoritySet(
        control_plane=control_plane,
        measurement_producer=measurement,
        source_artifact_producer=source,
        project_owner=owner,
        independent_verifier=verifier,
    )
    references = [
        _ref(
            evidence_class="authority_context",
            artifact=store.publish(context),
            producer_role="control_plane",
            producer_id=control_plane.producer_id,
            producer_hash=control_plane.producer_hash,
        ),
        _ref(
            evidence_class="round_authority",
            artifact=store.publish(round_authority),
            producer_role="control_plane",
            producer_id=control_plane.producer_id,
            producer_hash=control_plane.producer_hash,
        ),
        _ref(
            evidence_class="search_plan",
            artifact=store.artifact_for_hash(context.search_plan_hash),
            producer_role="control_plane",
            producer_id=control_plane.producer_id,
            producer_hash=control_plane.producer_hash,
        ),
        _ref(
            evidence_class="source_family",
            artifact=store.artifact_for_hash(context.candidate_family_hash),
            producer_role="source_artifact_producer",
            producer_id=source.producer_id,
            producer_hash=source.producer_hash,
        ),
        _ref(
            evidence_class="artifact_family",
            artifact=store.artifact_for_hash(context.artifact_family_hash),
            producer_role="source_artifact_producer",
            producer_id=source.producer_id,
            producer_hash=source.producer_hash,
        ),
        _ref(
            evidence_class="barrier",
            artifact=store.publish(search_barrier),
            producer_role="independent_verifier",
            producer_id=verifier.verifier_id,
            producer_hash=verifier.identity_hash,
        ),
    ]
    if reveal is not None and holdout_barrier is not None and comparison is not None:
        references.extend(
            [
                _ref(
                    evidence_class="holdout_reveal",
                    artifact=store.publish(reveal),
                    producer_role="holdout_plan_authority",
                    producer_id=context.holdout_plan_authority_id,
                    producer_hash=context.holdout_plan_authority_hash,
                ),
                _ref(
                    evidence_class="barrier",
                    artifact=store.publish(holdout_barrier),
                    producer_role="independent_verifier",
                    producer_id=verifier.verifier_id,
                    producer_hash=verifier.identity_hash,
                ),
                _ref(
                    evidence_class="fwer",
                    artifact=store.publish(comparison),
                    producer_role="independent_verifier",
                    producer_id=verifier.verifier_id,
                    producer_hash=verifier.identity_hash,
                ),
            ]
        )
    bundle_ref = _ref(
        evidence_class="evidence_bundle",
        artifact=store.publish(bundle),
        producer_role="independent_verifier",
        producer_id=verifier.verifier_id,
        producer_hash=verifier.identity_hash,
    )
    references.append(bundle_ref)

    signoff_intent = build_formal_round_signoff_intent(
        request=FormalRoundSignoffRequest(
            round_id=context.round_id,
            round_evidence_bundle_id=bundle.round_evidence_bundle_id,
            decision="approved",
            actor="formal-project-owner",
            actor_identity_hash=_hash("9"),
            reason="Approve this exact Formal terminal evidence path.",
            idempotency_key="formal-acceptance-signoff-v1",
        ),
        task_id=context.task_id,
        authority_context=formal_authority_context_ref(context),
        evidence_bundle_hash=bundle_ref.sha256,
        candidate_family_hash=context.candidate_family_hash,
        artifact_family_hash=context.artifact_family_hash,
        holdout_family_hash=bundle.holdout_family_hash,
        decision_at=NOW,
    )
    signoff_content = formal_round_signoff_artifact_content(signoff_intent)
    signoff_signature = _OwnerSigner(
        owner, wrong_identity=wrong_owner_identity
    ).sign(canonical_json_bytes(signoff_content))
    signoff_artifact = FormalRoundSignoffDecisionArtifact(
        content=signoff_content,
        signature=signoff_signature,
    )
    signoff_ref = _ref(
        evidence_class="signoff",
        artifact=store.publish(signoff_artifact),
        producer_role="project_owner",
        producer_id=owner.producer_id,
        producer_hash=owner.producer_hash,
    )
    references.append(signoff_ref)

    snapshot = FormalEvidenceAcceptanceSnapshot(
        round_id=context.round_id,
        readiness_audit_id="formal-readiness-v3",
        readiness_audit_base_commit="2" * 40,
        readiness_manifest_hash=_hash("a"),
        readiness_report_hash=_hash("b"),
        target_lock_hash=_hash("c"),
        terminal_path="holdout_fwer" if holdout else "zero_promotion",
        authority_context_id=context.authority_context_id,
        authority_context_hash=context.context_hash,
        round_evidence_bundle_id=bundle.round_evidence_bundle_id,
        round_evidence_bundle_hash=bundle_ref.sha256,
        signoff_artifact_hash=signoff_ref.sha256,
        signoff_signer_identity_hash=owner.producer_hash,
        evidence_root=root,
        verifier=verifier,
        authorities=authorities,
        references=tuple(references),
    )
    reader = _SnapshotReader(snapshot)
    object_verifier = _ObjectVerifier(store, root, verifier)
    review_verifier = _ReviewVerifier(verifier)
    service = FormalEvidenceAcceptanceService(
        snapshot_reader=reader,
        object_verifier=object_verifier,
        round_finalizer=M2FormalRoundFinalizer(store),
        signoff_finalizer=M2FormalRoundSignoffFinalizer(store, _OwnerVerifier()),
        summary_publisher=_SummaryPublisher(store),
        review_signer=_ReviewSigner(verifier),
        review_signature_verifier=review_verifier,
        clock=lambda: NOW,
    )
    return _AcceptanceFixture(service, snapshot, reader, store, bundle_ref)


@pytest.mark.parametrize("holdout", [False, True])
def test_accepts_only_recursively_verified_and_signed_terminal_paths(holdout: bool) -> None:
    fixture = _acceptance_fixture(holdout=holdout)

    review = fixture.service.review(
        round_id=fixture.snapshot.round_id,
        readiness_audit_id=fixture.snapshot.readiness_audit_id,
    )

    assert review.decision == "accepted_for_formal_window"
    assert review.schema_version == "m2a-formal-evidence-acceptance-review-v2"
    assert review.blocker_codes == ()
    assert review.recursive_semantic_verification == "verified"
    assert review.allowlisted_signature_verification == "verified"
    assert review.verified_evidence_count == len(fixture.snapshot.references)
    assert review.owner_window_authorization == "not_granted"
    assert review.hcu_accessed is False
    assert review.automatic_release_allowed is False


def test_tamper_missing_evidence_and_cross_round_fail_closed() -> None:
    tampered = _acceptance_fixture()
    tampered.store.tamper(tampered.bundle_ref.uri)
    tampered_review = tampered.service.review(
        round_id=tampered.snapshot.round_id,
        readiness_audit_id=tampered.snapshot.readiness_audit_id,
    )
    assert tampered_review.decision == "blocked"
    assert tampered_review.blocker_codes == ("evidence_hash_mismatch",)

    missing = _acceptance_fixture(holdout=True)
    missing.reader.snapshot = missing.snapshot.model_copy(
        update={
            "references": tuple(
                item for item in missing.snapshot.references if item.evidence_class != "fwer"
            )
        }
    )
    missing_review = missing.service.review(
        round_id=missing.snapshot.round_id,
        readiness_audit_id=missing.snapshot.readiness_audit_id,
    )
    assert missing_review.decision == "blocked"
    assert missing_review.blocker_codes == ("formal_fwer_evidence_incomplete",)

    crossed = _acceptance_fixture()
    with pytest.raises(FormalEvidenceAcceptanceError) as cross_round:
        crossed.service.review(
            round_id=UUID(int=999),
            readiness_audit_id=crossed.snapshot.readiness_audit_id,
        )
    assert cross_round.value.code == "formal_acceptance_snapshot_identity_mismatch"


def test_wrong_project_owner_identity_blocks_after_semantic_verification() -> None:
    fixture = _acceptance_fixture(wrong_owner_identity=True)

    review = fixture.service.review(
        round_id=fixture.snapshot.round_id,
        readiness_audit_id=fixture.snapshot.readiness_audit_id,
    )

    assert review.decision == "blocked"
    assert review.blocker_codes == ("formal_signoff_authority_mismatch",)
    assert review.recursive_semantic_verification == "verified"
    assert review.allowlisted_signature_verification == "blocked"


def test_d_review_verifier_rejects_signature_tamper_and_identity_drift() -> None:
    fixture = _acceptance_fixture()
    review = fixture.service.review(
        round_id=fixture.snapshot.round_id,
        readiness_audit_id=fixture.snapshot.readiness_audit_id,
    )
    verifier = _ReviewVerifier(fixture.snapshot.verifier)
    bad_value = review.model_copy(
        update={"signature": review.signature.model_copy(update={"value": "0" * 64})}
    )
    with pytest.raises(FormalEvidenceAcceptanceError) as invalid:
        verify_formal_evidence_acceptance_review_signature(bad_value, verifier)
    assert invalid.value.code == "formal_acceptance_review_signature_invalid"

    bad_hash = review.model_copy(update={"review_hash": _hash("f")})
    with pytest.raises(FormalEvidenceAcceptanceError) as changed:
        verify_formal_evidence_acceptance_review_signature(bad_hash, verifier)
    assert changed.value.code == "formal_acceptance_review_hash_mismatch"

    verifier.verifier_key_id = "unapproved-review-key"
    with pytest.raises(FormalEvidenceAcceptanceError) as not_allowlisted:
        verify_formal_evidence_acceptance_review_signature(review, verifier)
    assert not_allowlisted.value.code == "formal_acceptance_review_signer_not_allowlisted"
