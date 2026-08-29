# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from hcuopt.contracts.m2_formal_authority_v1 import (
    FormalAuthorityContextRef,
    FormalEvidenceStoreRef,
)
from hcuopt.contracts.m2_formal_signoff_v1 import (
    FormalDecisionSignature,
    FormalRoundSignoffArtifactPublication,
    FormalRoundSignoffDecisionArtifact,
    FormalRoundSignoffRequest,
    build_formal_round_signoff_intent,
    formal_round_signoff_artifact_content,
)
from hcuopt.evaluation.evidence_reader import EvidenceReadError
from hcuopt.evaluation.m2_formal_signoff import (
    LocalFormalSignoffArtifactPublisher,
    M2FormalRoundSignoffFinalizer,
    M2FormalSignoffError,
)
from hcuopt.measurement.evidence import canonical_json_bytes, write_evidence_bytes
from hcuopt.source_hash import file_uri_to_path

NOW = datetime(2026, 8, 29, 14, 0, tzinfo=timezone.utc)
KEY = b"m2-formal-signoff-test-key"


def _hash(character: str) -> str:
    return "sha256:" + character * 64


class _DeterministicSigner:
    def sign(self, payload: bytes) -> FormalDecisionSignature:
        return FormalDecisionSignature(
            signer_id="formal-signoff-test-signer",
            signer_key_id="test-key-v1",
            signer_identity_hash=_hash("a"),
            algorithm="test-sha256-v1",
            value=hashlib.sha256(KEY + payload).hexdigest(),
        )


class _DeterministicVerifier:
    def verify(self, payload: bytes, signature: FormalDecisionSignature) -> None:
        expected = hashlib.sha256(KEY + payload).hexdigest()
        if signature.value != expected:
            raise ValueError("invalid test signature")


class _LocalReader:
    def read_raw_bytes(self, uri: str, expected_hash: str) -> bytes:
        path = file_uri_to_path(uri)
        if path.is_symlink() or not path.is_file():
            raise EvidenceReadError("evidence_unreadable", "test evidence missing")
        encoded = path.read_bytes()
        actual = "sha256:" + hashlib.sha256(encoded).hexdigest()
        if actual != expected_hash:
            raise EvidenceReadError("evidence_hash_mismatch", "test hash mismatch")
        return encoded


def _intent(*, decision: str = "approved", idempotency_key: str = "signoff-key-001"):
    round_id = UUID(int=1)
    task_id = UUID(int=2)
    context = FormalAuthorityContextRef(
        authority_context_id=UUID(int=3),
        context_hash=_hash("1"),
        round_id=round_id,
        task_id=task_id,
        candidate_family_hash=_hash("2"),
        artifact_family_hash=_hash("3"),
        search_plan_hash=_hash("4"),
        holdout_plan_commitment=_hash("5"),
        selection_rule_hash=_hash("6"),
        evidence_store=FormalEvidenceStoreRef(
            store_id="formal-store",
            store_version=1,
            store_hash=_hash("7"),
            access_policy_hash=_hash("8"),
        ),
    )
    request = FormalRoundSignoffRequest(
        round_id=round_id,
        round_evidence_bundle_id=UUID(int=4),
        decision=decision,
        actor="operator-a",
        actor_identity_hash=_hash("9"),
        reason="Accept the complete Formal Round evidence.",
        idempotency_key=idempotency_key,
    )
    return build_formal_round_signoff_intent(
        request=request,
        task_id=task_id,
        authority_context=context,
        evidence_bundle_hash=_hash("b"),
        candidate_family_hash=context.candidate_family_hash,
        artifact_family_hash=context.artifact_family_hash,
        holdout_family_hash=None,
        decision_at=NOW,
    )


def test_intent_and_signed_artifact_are_deterministic_and_never_release(
    tmp_path: Path,
) -> None:
    first = _intent()
    second = _intent()
    assert first == second
    assert not first.synthetic
    assert not first.automatic_release_allowed

    publisher = LocalFormalSignoffArtifactPublisher(tmp_path, _DeterministicSigner())
    first_publication = publisher.publish(first)
    second_publication = publisher.publish(second)

    assert first_publication == second_publication
    artifact = M2FormalRoundSignoffFinalizer(
        _LocalReader(), _DeterministicVerifier()
    ).verify(intent=first, publication=first_publication)
    assert artifact.content.performance_scope == "formal_single_operation_only"
    assert not artifact.content.automatic_release_allowed


def test_intent_identity_changes_with_decision_or_idempotency_key() -> None:
    approved = _intent()
    rejected = _intent(decision="rejected")
    replay_key_changed = _intent(idempotency_key="signoff-key-002")

    assert approved.input_digest != rejected.input_digest
    assert approved.signoff_intent_id != rejected.signoff_intent_id
    assert approved.input_digest != replay_key_changed.input_digest


def test_finalizer_rejects_tampering_and_wrong_intent(tmp_path: Path) -> None:
    intent = _intent()
    publisher = LocalFormalSignoffArtifactPublisher(tmp_path, _DeterministicSigner())
    publication = publisher.publish(intent)
    path = file_uri_to_path(publication.decision_artifact_uri)
    path.chmod(0o600)
    path.write_bytes(b'{"tampered":true}\n')

    finalizer = M2FormalRoundSignoffFinalizer(_LocalReader(), _DeterministicVerifier())
    with pytest.raises(M2FormalSignoffError) as captured:
        finalizer.verify(intent=intent, publication=publication)
    assert captured.value.code == "evidence_hash_mismatch"

    different = _intent(idempotency_key="signoff-key-003")
    with pytest.raises(
        M2FormalSignoffError, match="belongs to another Intent"
    ) as wrong_intent:
        finalizer.verify(intent=different, publication=publication)
    assert wrong_intent.value.code == "round_signoff_artifact_identity_mismatch"


def test_finalizer_rejects_invalid_signature(tmp_path: Path) -> None:
    intent = _intent()
    publication = LocalFormalSignoffArtifactPublisher(
        tmp_path, _DeterministicSigner()
    ).publish(intent)

    class _RejectingVerifier:
        def verify(self, payload: bytes, signature: FormalDecisionSignature) -> None:
            raise ValueError("signature rejected")

    with pytest.raises(M2FormalSignoffError) as captured:
        M2FormalRoundSignoffFinalizer(_LocalReader(), _RejectingVerifier()).verify(
            intent=intent,
            publication=publication,
        )
    assert captured.value.code == "round_signoff_signature_invalid"


@pytest.mark.parametrize(
    "changed_input",
    [
        {"evidence_bundle_hash": _hash("c")},
        {"actor": "other-operator"},
        {"actor_identity_hash": _hash("d")},
        {"reason": "A different human decision reason."},
    ],
)
def test_finalizer_rejects_resigned_frozen_input_drift(
    tmp_path: Path,
    changed_input: dict[str, str],
) -> None:
    intent = _intent()
    content = formal_round_signoff_artifact_content(intent).model_copy(
        update=changed_input
    )
    signer = _DeterministicSigner()
    signature = signer.sign(canonical_json_bytes(content))
    artifact = FormalRoundSignoffDecisionArtifact(
        content=content,
        signature=signature,
    )
    published = write_evidence_bytes(
        tmp_path / "forged-signoff-decision.json",
        canonical_json_bytes(artifact),
    )
    publication = FormalRoundSignoffArtifactPublication(
        signoff_intent_id=intent.signoff_intent_id,
        round_signoff_id=intent.round_signoff_id,
        decision_artifact_uri=published.uri,
        decision_artifact_hash=published.sha256,
        signature=signature,
    )

    with pytest.raises(M2FormalSignoffError) as captured:
        M2FormalRoundSignoffFinalizer(_LocalReader(), _DeterministicVerifier()).verify(
            intent=intent,
            publication=publication,
        )
    assert captured.value.code == "round_signoff_evidence_mismatch"
