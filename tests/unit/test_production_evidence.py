# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from hcuopt.contracts.formal_evidence_acceptance_v1 import (
    FormalEvidenceAcceptanceReviewContent,
    FormalEvidenceAcceptanceReviewSignature,
    FormalEvidenceAuthoritySet,
    FormalEvidenceVerifierIdentityContent,
    FormalProducerIdentity,
    ProductionEvidenceRootContent,
    ProductionFormalEvidenceRef,
    publish_formal_evidence_acceptance_review,
    publish_formal_evidence_verifier_identity,
    publish_production_evidence_root,
)
from hcuopt.contracts.m2_formal_authority_v1 import (
    FormalAuthorityContextContent,
    FormalEvidenceStoreRef,
    FormalVerifierRef,
    publish_formal_authority_context,
)
from hcuopt.evaluation.production_evidence import (
    ProductionEvidenceError,
    ProductionEvidenceVerifier,
    content_addressed_evidence_path,
)
from hcuopt.measurement.evidence import canonical_json_bytes

NOW = datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)


def _hash(encoded: bytes) -> str:
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _fixed(character: str) -> str:
    return "sha256:" + character * 64


def _publish(root: Path, value: object) -> tuple[str, str, int]:
    encoded = canonical_json_bytes(value)
    digest = _hash(encoded)
    path = content_addressed_evidence_path(root, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return path.as_uri(), digest, len(encoded)


def _producer(role: str, character: str, capability: str) -> FormalProducerIdentity:
    return FormalProducerIdentity(
        producer_role=role,
        producer_id=f"{role}-authority",
        producer_hash=_fixed(character),
        capability=capability,
    )


def _fixture(root: Path):
    root.mkdir(parents=True)
    identity_uri, identity_evidence_hash, _ = _publish(root, {"identity": "d-verifier"})
    verifier = publish_formal_evidence_verifier_identity(
        FormalEvidenceVerifierIdentityContent(
            verifier_id="m2a-d-production-verifier",
            verifier_version="1.0.0",
            executable_hash=_fixed("1"),
            configuration_hash=_fixed("2"),
            source_commit="1" * 40,
            attestation_scheme="ed25519-v1",
            attestation_key_id="deployment/d/formal/verifier-1",
            identity_evidence_uri=identity_uri,
            identity_evidence_hash=identity_evidence_hash,
        )
    )
    root_descriptor = publish_production_evidence_root(
        ProductionEvidenceRootContent(
            root_id="nmz36-formal-evidence",
            root_version=1,
            root_uri=root.as_uri(),
            access_policy_hash=_fixed("3"),
            retention_policy_hash=_fixed("4"),
            deployment_owner="deployment-evidence-service",
            max_object_bytes=1024 * 1024,
        )
    )
    authorities = FormalEvidenceAuthoritySet(
        control_plane=_producer("control_plane", "5", "formal_control_plane"),
        measurement_producer=_producer("measurement_producer", "6", "formal_measurement"),
        source_artifact_producer=_producer(
            "source_artifact_producer", "7", "formal_source_artifact"
        ),
        project_owner=_producer("project_owner", "8", "formal_signoff"),
        independent_verifier=verifier,
    )
    context = publish_formal_authority_context(
        FormalAuthorityContextContent(
            authority_context_id=UUID(int=1),
            round_id=UUID(int=2),
            task_id=UUID(int=3),
            target_snapshot_id=UUID(int=4),
            stage0_run_id=UUID(int=5),
            stage0_protocol_hash=_fixed("9"),
            baseline_epoch_id=UUID(int=6),
            hotspot_id=UUID(int=7),
            target_profile_hash=_fixed("a"),
            workload_profile_hash=_fixed("b"),
            measurement_profile_hash=_fixed("c"),
            candidate_family_hash=_fixed("d"),
            artifact_family_hash=_fixed("e"),
            search_plan_hash=_fixed("f"),
            holdout_plan_commitment=_fixed("0"),
            holdout_plan_authority_id="d-holdout-plan-authority",
            holdout_plan_authority_hash=_fixed("1"),
            selection_rule_hash=_fixed("2"),
            evidence_store=FormalEvidenceStoreRef(
                store_id=root_descriptor.root_id,
                store_version=root_descriptor.root_version,
                store_hash=root_descriptor.root_hash,
                access_policy_hash=root_descriptor.access_policy_hash,
            ),
            verifier=FormalVerifierRef(
                verifier_id=verifier.verifier_id,
                verifier_version=verifier.verifier_version,
                verifier_hash=verifier.identity_hash,
            ),
            sealed_by="formal-control-plane",
            sealed_at=NOW,
        )
    )
    refs = []
    specs = (
        ("search_plan", "control_plane", authorities.control_plane),
        ("round_authority", "control_plane", authorities.control_plane),
        ("search_measurement", "measurement_producer", authorities.measurement_producer),
        ("source_family", "source_artifact_producer", authorities.source_artifact_producer),
        ("barrier", "independent_verifier", verifier),
        ("signoff", "project_owner", authorities.project_owner),
    )
    for ordinal, (evidence_class, role, identity) in enumerate(specs):
        uri, digest, byte_count = _publish(root, {"evidence": evidence_class, "n": ordinal})
        identity_id = (
            identity.verifier_id if role == "independent_verifier" else identity.producer_id
        )
        identity_hash = (
            identity.identity_hash if role == "independent_verifier" else identity.producer_hash
        )
        refs.append(
            ProductionFormalEvidenceRef(
                evidence_class=evidence_class,
                uri=uri,
                sha256=digest,
                byte_count=byte_count,
                producer_role=role,
                producer_id=identity_id,
                producer_hash=identity_hash,
                created_at=NOW,
            )
        )
    return root_descriptor, verifier, authorities, context, tuple(refs)


@pytest.mark.skipif(os.name != "posix", reason="production reader requires POSIX openat")
def test_reloads_content_addressed_evidence_and_is_order_independent(tmp_path: Path) -> None:
    root, verifier_identity, authorities, context, refs = _fixture(tmp_path / "evidence")
    verifier = ProductionEvidenceVerifier(
        tmp_path / "evidence", root=root, verifier=verifier_identity
    )

    first = verifier.verify(context=context, authorities=authorities, references=refs)
    second = verifier.verify(
        context=context, authorities=authorities, references=tuple(reversed(refs))
    )

    assert first == second
    assert first.status == "objects_verified"
    assert first.accepted_for_formal_window is False
    assert first.acceptance_blocker_codes == (
        "recursive_semantic_verification_not_bound",
        "allowlisted_signature_verification_not_bound",
    )
    assert first.hcu_accessed is False
    assert first.automatic_release_allowed is False


@pytest.mark.skipif(os.name != "posix", reason="production reader requires POSIX openat")
def test_rejects_arbitrary_path_and_tampered_object(tmp_path: Path) -> None:
    root, verifier_identity, authorities, context, refs = _fixture(tmp_path / "evidence")
    verifier = ProductionEvidenceVerifier(
        tmp_path / "evidence", root=root, verifier=verifier_identity
    )
    arbitrary = tmp_path / "evidence" / "arbitrary.json"
    arbitrary.write_bytes(canonical_json_bytes({"not": "content-addressed"}))
    changed = refs[0].model_copy(update={"uri": arbitrary.as_uri()})
    with pytest.raises(ProductionEvidenceError) as rejected:
        verifier.verify(context=context, authorities=authorities, references=(changed, *refs[1:]))
    assert rejected.value.code == "formal_evidence_uri_not_content_addressed"

    content_path = Path(refs[0].uri.removeprefix("file://"))
    content_path.write_bytes(canonical_json_bytes({"tampered": True}))
    with pytest.raises(ProductionEvidenceError) as tampered:
        verifier.verify(context=context, authorities=authorities, references=refs)
    assert tampered.value.code == "evidence_hash_mismatch"


@pytest.mark.skipif(os.name != "posix", reason="production reader requires POSIX openat")
def test_rejects_role_substitution_and_cross_stage_reuse(tmp_path: Path) -> None:
    root, verifier_identity, authorities, context, refs = _fixture(tmp_path / "evidence")
    verifier = ProductionEvidenceVerifier(
        tmp_path / "evidence", root=root, verifier=verifier_identity
    )
    substituted = refs[1].model_copy(
        update={
            "producer_role": "independent_verifier",
            "producer_id": verifier_identity.verifier_id,
            "producer_hash": verifier_identity.identity_hash,
        }
    )
    with pytest.raises(ProductionEvidenceError) as wrong_role:
        verifier.verify(
            context=context, authorities=authorities, references=(refs[0], substituted, *refs[2:])
        )
    assert wrong_role.value.code == "formal_evidence_role_mismatch"

    reused = refs[0].model_copy(
        update={
            "evidence_class": "holdout_reveal",
            "producer_role": "holdout_plan_authority",
            "producer_id": context.holdout_plan_authority_id,
            "producer_hash": context.holdout_plan_authority_hash,
        }
    )
    with pytest.raises(ProductionEvidenceError) as cross_stage:
        verifier.verify(context=context, authorities=authorities, references=(refs[0], reused))
    assert cross_stage.value.code == "formal_evidence_cross_stage_reuse"


@pytest.mark.skipif(os.name != "posix", reason="production reader requires POSIX openat")
def test_holdout_reveal_requires_context_bound_d_authority(tmp_path: Path) -> None:
    root, verifier_identity, authorities, context, refs = _fixture(tmp_path / "evidence")
    verifier = ProductionEvidenceVerifier(
        tmp_path / "evidence", root=root, verifier=verifier_identity
    )
    uri, digest, byte_count = _publish(tmp_path / "evidence", {"reveal": "receipt"})
    reveal = ProductionFormalEvidenceRef(
        evidence_class="holdout_reveal",
        uri=uri,
        sha256=digest,
        byte_count=byte_count,
        producer_role="holdout_plan_authority",
        producer_id=context.holdout_plan_authority_id,
        producer_hash=context.holdout_plan_authority_hash,
        created_at=NOW,
    )

    result = verifier.verify(
        context=context,
        authorities=authorities,
        references=(*refs, reveal),
    )
    assert result.status == "objects_verified"

    control_plane_reveal = reveal.model_copy(
        update={
            "producer_role": "control_plane",
            "producer_id": authorities.control_plane.producer_id,
            "producer_hash": authorities.control_plane.producer_hash,
        }
    )
    with pytest.raises(ProductionEvidenceError) as rejected:
        verifier.verify(
            context=context,
            authorities=authorities,
            references=(*refs, control_plane_reveal),
        )
    assert rejected.value.code == "formal_evidence_role_mismatch"


@pytest.mark.skipif(os.name != "posix", reason="production reader requires POSIX openat")
def test_holdout_authority_must_not_alias_control_plane(tmp_path: Path) -> None:
    root, verifier_identity, authorities, context, refs = _fixture(tmp_path / "evidence")
    aliased = context.model_copy(
        update={
            "holdout_plan_authority_id": authorities.control_plane.producer_id,
            "holdout_plan_authority_hash": authorities.control_plane.producer_hash,
        }
    )
    verifier = ProductionEvidenceVerifier(
        tmp_path / "evidence", root=root, verifier=verifier_identity
    )
    with pytest.raises(ProductionEvidenceError) as rejected:
        verifier.verify(context=aliased, authorities=authorities, references=refs)
    assert rejected.value.code == "holdout_plan_authority_not_separated"


def test_rejects_producer_verifier_identity_collision(tmp_path: Path) -> None:
    _root, verifier_identity, authorities, _context, _refs = _fixture(tmp_path / "evidence")
    collision = authorities.measurement_producer.model_copy(
        update={
            "producer_id": verifier_identity.verifier_id,
            "producer_hash": verifier_identity.identity_hash,
        }
    )
    with pytest.raises(ValidationError, match="role-separated"):
        FormalEvidenceAuthoritySet(
            control_plane=authorities.control_plane,
            measurement_producer=collision,
            source_artifact_producer=authorities.source_artifact_producer,
            project_owner=authorities.project_owner,
            independent_verifier=verifier_identity,
        )


@pytest.mark.skipif(os.name != "posix", reason="production reader requires POSIX openat")
def test_context_and_verifier_identity_drift_fail_closed(tmp_path: Path) -> None:
    root, verifier_identity, authorities, context, refs = _fixture(tmp_path / "evidence")
    verifier = ProductionEvidenceVerifier(
        tmp_path / "evidence", root=root, verifier=verifier_identity
    )
    changed_store = context.evidence_store.model_copy(update={"store_hash": _fixed("8")})
    drifted_context = context.model_copy(update={"evidence_store": changed_store})
    with pytest.raises(ProductionEvidenceError) as root_drift:
        verifier.verify(context=drifted_context, authorities=authorities, references=refs)
    assert root_drift.value.code == "formal_evidence_root_binding_mismatch"

    drifted_authorities = authorities.model_copy(
        update={
            "independent_verifier": verifier_identity.model_copy(
                update={"configuration_hash": _fixed("7")}
            )
        }
    )
    with pytest.raises(ProductionEvidenceError) as verifier_drift:
        verifier.verify(context=context, authorities=drifted_authorities, references=refs)
    assert verifier_drift.value.code == "formal_verifier_identity_drift"


def test_d_review_record_requires_verified_evidence_or_explicit_blockers(tmp_path: Path) -> None:
    root, verifier_identity, authorities, context, refs = _fixture(tmp_path / "evidence")
    summary = refs[0]
    base = {
        "review_id": "m2a-d-review-0001",
        "decision": "blocked",
        "blocker_codes": (
            "allowlisted_signature_verification_not_bound",
            "recursive_semantic_verification_not_bound",
        ),
        "reason": "Recursive semantic and signature verification are not bound yet.",
        "readiness_audit_id": "nmz36-formal-readiness-v2",
        "readiness_audit_base_commit": "2" * 40,
        "readiness_manifest_hash": _fixed("3"),
        "readiness_report_hash": _fixed("4"),
        "round_id": context.round_id,
        "authority_context_id": context.authority_context_id,
        "authority_context_hash": context.context_hash,
        "target_lock_hash": _fixed("5"),
        "terminal_path": "zero_promotion",
        "round_evidence_bundle_id": UUID(int=8),
        "round_evidence_bundle_hash": _fixed("6"),
        "signoff_artifact_hash": _fixed("7"),
        "signoff_signer_identity_hash": authorities.project_owner.producer_hash,
        "evidence_root": root,
        "verifier": verifier_identity,
        "verification_input_digest": _fixed("8"),
        "verified_evidence_count": len(refs),
        "recursive_semantic_verification": "blocked",
        "allowlisted_signature_verification": "blocked",
        "verification_summary_uri": summary.uri,
        "verification_summary_hash": summary.sha256,
        "reviewed_at": NOW,
    }
    content = FormalEvidenceAcceptanceReviewContent(**base)
    signature = FormalEvidenceAcceptanceReviewSignature(
        verifier_id=verifier_identity.verifier_id,
        verifier_key_id=verifier_identity.attestation_key_id,
        verifier_identity_hash=verifier_identity.identity_hash,
        algorithm=verifier_identity.attestation_scheme,
        value="a" * 64,
    )
    review = publish_formal_evidence_acceptance_review(content, signature=signature)
    assert review.decision == "blocked"
    assert review.owner_window_authorization == "not_granted"

    accepted = FormalEvidenceAcceptanceReviewContent.model_validate(
        {
            **base,
            "decision": "accepted_for_formal_window",
            "blocker_codes": (),
            "recursive_semantic_verification": "verified",
            "allowlisted_signature_verification": "verified",
        }
    )
    assert accepted.decision == "accepted_for_formal_window"

    with pytest.raises(ValidationError, match="requires recursive semantics"):
        FormalEvidenceAcceptanceReviewContent.model_validate(
            {
                **base,
                "decision": "accepted_for_formal_window",
                "blocker_codes": (),
            }
        )
    with pytest.raises(ValidationError, match="requires explicit blocker"):
        FormalEvidenceAcceptanceReviewContent.model_validate(
            {
                **base,
                "decision": "blocked",
                "blocker_codes": (),
            }
        )


@pytest.mark.skipif(os.name != "posix", reason="production reader requires POSIX openat")
def test_missing_object_and_verifier_attestation_fail_closed(tmp_path: Path) -> None:
    root, verifier_identity, authorities, context, refs = _fixture(tmp_path / "evidence")
    verifier = ProductionEvidenceVerifier(
        tmp_path / "evidence", root=root, verifier=verifier_identity
    )
    Path(refs[0].uri.removeprefix("file://")).unlink()
    with pytest.raises(ProductionEvidenceError) as missing:
        verifier.verify(context=context, authorities=authorities, references=refs)
    assert missing.value.code == "formal_evidence_missing"

    root2, verifier_identity2, authorities2, context2, refs2 = _fixture(
        tmp_path / "identity-tamper"
    )
    identity_path = Path(verifier_identity2.identity_evidence_uri.removeprefix("file://"))
    identity_path.write_bytes(canonical_json_bytes({"identity": "producer-substitution"}))
    verifier2 = ProductionEvidenceVerifier(
        tmp_path / "identity-tamper", root=root2, verifier=verifier_identity2
    )
    with pytest.raises(ProductionEvidenceError) as identity_tamper:
        verifier2.verify(context=context2, authorities=authorities2, references=refs2)
    assert identity_tamper.value.code == "evidence_hash_mismatch"
