# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from hcuopt.contracts.m2_formal_start_v1 import FormalStartSignerRef
from hcuopt.domain.errors import NotFound
from hcuopt.evaluation.m2_formal_start_authority import (
    M2FormalEvaluationStartAuthorityError,
    M2FormalEvaluationStartAuthorityIssuer,
    M2FormalEvaluationStartRegistration,
)
from tests.unit.test_formal_operator_plans import NOW, _fixture, _hash
from tests.unit.test_formal_operator_start import (
    D_SIGNER,
    _authorities,
    _coordinator,
    _ObjectStore,
    _production_evidence,
    _Repository,
    _request,
)


@dataclass
class PreviewStore:
    preview: object
    load_count: int = 0

    def load_preview(self, preview_id: UUID):  # type: ignore[no-untyped-def]
        self.load_count += 1
        if self.preview.preview_id != preview_id:  # type: ignore[attr-defined]
            raise NotFound("missing Preview")
        return self.preview


@dataclass
class EvaluationRegistry:
    registration: M2FormalEvaluationStartRegistration | None
    load_count: int = 0

    def load_registration(
        self,
        *,
        preview_id: UUID,
        formal_authorization_hash: str,
        resolved_plan_hash: str,
    ) -> M2FormalEvaluationStartRegistration:
        self.load_count += 1
        value = self.registration
        if (
            value is None
            or value.preview_id != preview_id
            or value.formal_authorization_hash != formal_authorization_hash
            or value.resolved_plan_hash != resolved_plan_hash
        ):
            raise NotFound("missing registration")
        return value


@dataclass
class AuthorizationVerifier:
    verifier_ref: object
    accepted: bool = True
    raises: bool = False

    def verify_signature(self, *, authorization_hash: str, signature: str) -> bool:
        assert authorization_hash.startswith("sha256:")
        assert signature
        if self.raises:
            raise RuntimeError("verifier unavailable")
        return self.accepted


@dataclass
class StartSigner:
    signer_ref: FormalStartSignerRef = D_SIGNER
    raises: bool = False
    signed_hashes: tuple[str, ...] = ()

    def sign_authority(self, *, content_hash: str) -> str:
        if self.raises:
            raise RuntimeError("signer unavailable")
        self.signed_hashes = (*self.signed_hashes, content_hash)
        return "formal-evaluation-start-signature"


def _registration(fixture, preview, tmp_path):  # type: ignore[no-untyped-def]
    plan = preview.resolved_plan
    root, independent_verifier = _production_evidence(tmp_path)
    return M2FormalEvaluationStartRegistration(
        registration_id=uuid4(),
        preview_id=preview.preview_id,
        formal_authorization_hash=fixture.authorization.authorization_hash,
        resolved_plan_hash=preview.resolved_plan_hash,
        candidate_family_hash=plan.source_family_hash,
        search_protocol_hash=plan.search_protocol_hash,
        holdout_protocol_hash=plan.holdout_protocol_hash,
        search_plan_hash=_hash("formal-search-plan"),
        holdout_plan_commitment=_hash("formal-holdout-commitment"),
        holdout_plan_authority_id="formal-holdout-plan-authority",
        holdout_plan_authority_hash=_hash("holdout-plan-authority"),
        selection_rule_hash=plan.selection_rule_hash,
        family_alpha=0.05,
        evidence_root=root,
        independent_verifier=independent_verifier,
        host_id=fixture.authorization.host_id,
        resource_id=fixture.authorization.resource_id,
        window_starts_at=fixture.authorization.window_starts_at,
        window_expires_at=fixture.authorization.window_expires_at,
        registered_at=NOW,
        expires_at=fixture.authorization.window_expires_at,
    )


def _issuer(fixture, preview, registration, **updates):  # type: ignore[no-untyped-def]
    values = {
        "preview_store": PreviewStore(preview),
        "evaluation_registry": EvaluationRegistry(registration),
        "authorization_verifier": AuthorizationVerifier(fixture.authorization.verifier),
        "signer": StartSigner(),
        "clock": lambda: NOW,
    }
    values.update(updates)
    return M2FormalEvaluationStartAuthorityIssuer(**values)


def test_d_issuer_rereads_sealed_plan_and_feeds_formal_start(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    registration = _registration(fixture, preview, tmp_path)
    preview_store = PreviewStore(preview)
    registry = EvaluationRegistry(registration)
    signer = StartSigner()
    issuer = _issuer(
        fixture,
        preview,
        registration,
        preview_store=preview_store,
        evaluation_registry=registry,
        signer=signer,
    )

    issued = issuer.issue(
        preview_id=preview.preview_id,
        idempotency_key="formal-start-intent-test-v1",
    )

    assert issued.schema_version == "m2a-formal-evaluation-start-authority-v1"
    assert issued.preview_id == preview.preview_id
    assert issued.resolved_plan_hash == preview.resolved_plan_hash
    assert issued.candidate_family_hash == preview.resolved_plan.source_family_hash
    assert issued.search_plan_hash == registration.search_plan_hash
    assert issued.holdout_plan_commitment == registration.holdout_plan_commitment
    assert issued.selection_rule_hash == preview.resolved_plan.selection_rule_hash
    assert issued.evidence_root == registration.evidence_root
    assert issued.independent_verifier == registration.independent_verifier
    assert issued.expires_at == preview.expires_at
    assert issued.synthetic is False
    assert issued.automatic_release_allowed is False
    assert signer.signed_hashes == (issued.authority_hash,)
    assert preview_store.load_count == 1
    assert registry.load_count == 1

    execution, _unused_evaluation = _authorities(fixture, preview, tmp_path)
    result = _coordinator(
        fixture,
        _ObjectStore(preview, execution, issued),
    ).create(
        _request(fixture, preview, execution.authority_hash, issued.authority_hash),
        _Repository(fixture.repository),
    )

    assert result.state == "ready_for_round_creation"
    assert result.round_creation_allowed is False
    assert result.hcu_accessed is False


@pytest.mark.parametrize(
    "change",
    [
        {"host_id": "another-host"},
        {"resource_id": "hcu-0"},
        {"registered_at": NOW + timedelta(minutes=1)},
        {"selection_rule_hash": _hash("another-selection-rule")},
        {"candidate_family_hash": _hash("another-candidate-family")},
    ],
)
def test_d_issuer_rejects_registration_drift(
    tmp_path: Path,
    change: dict[str, object],
) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    registration = M2FormalEvaluationStartRegistration.model_validate(
        _registration(fixture, preview, tmp_path).model_dump(mode="json") | change
    )

    with pytest.raises(
        M2FormalEvaluationStartAuthorityError,
        match="registration drifted",
    ):
        _issuer(fixture, preview, registration).issue(
            preview_id=preview.preview_id,
            idempotency_key="formal-start-intent-test-v1",
        )


def test_d_issuer_fails_closed_for_missing_registration_or_owner_signature(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    registration = _registration(fixture, preview, tmp_path)

    with pytest.raises(M2FormalEvaluationStartAuthorityError, match="not registered"):
        _issuer(fixture, preview, None).issue(
            preview_id=preview.preview_id,
            idempotency_key="formal-start-intent-test-v1",
        )
    with pytest.raises(M2FormalEvaluationStartAuthorityError, match="signature was rejected"):
        _issuer(
            fixture,
            preview,
            registration,
            authorization_verifier=AuthorizationVerifier(
                fixture.authorization.verifier,
                accepted=False,
            ),
        ).issue(
            preview_id=preview.preview_id,
            idempotency_key="formal-start-intent-test-v1",
        )


def test_d_issuer_fails_closed_for_signer_failure_or_owner_role_reuse(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    registration = _registration(fixture, preview, tmp_path)

    with pytest.raises(M2FormalEvaluationStartAuthorityError, match="signer failed"):
        _issuer(
            fixture,
            preview,
            registration,
            signer=StartSigner(raises=True),
        ).issue(
            preview_id=preview.preview_id,
            idempotency_key="formal-start-intent-test-v1",
        )
    owner = fixture.authorization.verifier
    reused = FormalStartSignerRef(
        signer_id=owner.verifier_id,
        signer_version=owner.verifier_version,
        signer_hash=owner.verifier_hash,
        signature_scheme=owner.signature_scheme,
        key_id=owner.key_id,
    )
    with pytest.raises(M2FormalEvaluationStartAuthorityError, match="must be separate"):
        _issuer(
            fixture,
            preview,
            registration,
            signer=StartSigner(signer_ref=reused),
        ).issue(
            preview_id=preview.preview_id,
            idempotency_key="formal-start-intent-test-v1",
        )


def test_d_registration_rejects_holdout_and_verifier_role_reuse(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    registration = _registration(fixture, preview, tmp_path)
    verifier = registration.independent_verifier

    with pytest.raises(ValidationError, match="must be separate"):
        M2FormalEvaluationStartRegistration.model_validate(
            registration.model_dump(mode="json")
            | {
                "holdout_plan_authority_id": verifier.verifier_id,
                "holdout_plan_authority_hash": verifier.identity_hash,
            }
        )
