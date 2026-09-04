# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hcuopt.contracts.m2_formal_execution_v1 import (
    M2FormalExecutionAdapterProfileContent,
    publish_m2_formal_execution_adapter_profile,
)
from hcuopt.contracts.m2_formal_start_v1 import FormalStartSignerRef
from hcuopt.domain.errors import NotFound
from hcuopt.measurement.m2_formal_start_authority import (
    M2FormalExecutionProfileRegistration,
    M2FormalExecutionStartAuthorityError,
    M2FormalExecutionStartAuthorityIssuer,
)
from tests.unit.test_formal_operator_plans import NOW, _fixture, _hash
from tests.unit.test_formal_operator_start import (
    B_SIGNER,
    _authorities,
    _coordinator,
    _ObjectStore,
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
class ProfileRegistry:
    registration: M2FormalExecutionProfileRegistration | None
    load_count: int = 0

    def load_registration(
        self,
        *,
        adapter_profile_id: str,
        formal_authorization_hash: str,
    ) -> M2FormalExecutionProfileRegistration:
        self.load_count += 1
        value = self.registration
        if (
            value is None
            or value.profile.profile_id != adapter_profile_id
            or value.formal_authorization_hash != formal_authorization_hash
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
    signer_ref: FormalStartSignerRef = B_SIGNER
    raises: bool = False
    signed_hashes: tuple[str, ...] = ()

    def sign_authority(self, *, content_hash: str) -> str:
        if self.raises:
            raise RuntimeError("signer unavailable")
        self.signed_hashes = (*self.signed_hashes, content_hash)
        return "formal-execution-start-signature"


def _registration(fixture, preview):  # type: ignore[no-untyped-def]
    plan = preview.resolved_plan
    assert plan.authority is not None
    profile = publish_m2_formal_execution_adapter_profile(
        M2FormalExecutionAdapterProfileContent(
            profile_id=plan.authority.adapter_profile,
            profile_version="1.0.0",
        )
    )
    return M2FormalExecutionProfileRegistration(
        registration_id=uuid4(),
        profile=profile,
        formal_authorization_hash=fixture.authorization.authorization_hash,
        host_id=fixture.authorization.host_id,
        resource_id=fixture.authorization.resource_id,
        window_starts_at=fixture.authorization.window_starts_at,
        window_expires_at=fixture.authorization.window_expires_at,
        lease_policy_hash=_hash("lease-policy"),
        fencing_policy_hash=_hash("fencing-policy"),
        cleanup_policy_hash=_hash("cleanup-policy"),
        registered_at=NOW,
        expires_at=fixture.authorization.window_expires_at,
    )


def _issuer(fixture, preview, registration, **updates):  # type: ignore[no-untyped-def]
    values = {
        "preview_store": PreviewStore(preview),
        "profile_registry": ProfileRegistry(registration),
        "authorization_verifier": AuthorizationVerifier(fixture.authorization.verifier),
        "signer": StartSigner(),
        "clock": lambda: NOW,
    }
    values.update(updates)
    return M2FormalExecutionStartAuthorityIssuer(**values)


def test_b_issuer_rereads_registered_profile_and_feeds_formal_start(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    registration = _registration(fixture, preview)
    preview_store = PreviewStore(preview)
    registry = ProfileRegistry(registration)
    signer = StartSigner()
    issuer = _issuer(
        fixture,
        preview,
        registration,
        preview_store=preview_store,
        profile_registry=registry,
        signer=signer,
    )

    issued = issuer.issue(
        preview_id=preview.preview_id,
        idempotency_key="formal-start-intent-test-v1",
    )

    assert issued.schema_version == "m2a-formal-execution-start-authority-v2"
    assert issued.preview_id == preview.preview_id
    assert issued.resolved_plan_hash == preview.resolved_plan_hash
    assert issued.candidate_family_hash == preview.resolved_plan.source_family_hash
    assert issued.adapter_profile_hash == registration.profile.profile_hash
    assert issued.issued_at == NOW
    assert issued.synthetic is False
    assert issued.automatic_release_allowed is False
    assert signer.signed_hashes == (issued.authority_hash,)
    assert preview_store.load_count == 1
    assert registry.load_count == 1

    _unused_execution, evaluation = _authorities(
        fixture,
        preview,
        tmp_path,
    )
    result = _coordinator(
        fixture,
        _ObjectStore(preview, issued, evaluation),
    ).create(
        _request(fixture, preview, issued.authority_hash, evaluation.authority_hash),
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
    ],
)
def test_b_issuer_rejects_registration_drift(
    tmp_path: Path,
    change: dict[str, object],
) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    registration = M2FormalExecutionProfileRegistration.model_validate(
        _registration(fixture, preview).model_dump(mode="json") | change
    )

    with pytest.raises(
        M2FormalExecutionStartAuthorityError,
        match="registration drifted",
    ):
        _issuer(fixture, preview, registration).issue(
            preview_id=preview.preview_id,
            idempotency_key="formal-start-intent-test-v1",
        )


def test_b_issuer_fails_closed_for_missing_registration_or_owner_signature(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    registration = _registration(fixture, preview)

    with pytest.raises(M2FormalExecutionStartAuthorityError, match="not registered"):
        _issuer(fixture, preview, None).issue(
            preview_id=preview.preview_id,
            idempotency_key="formal-start-intent-test-v1",
        )
    with pytest.raises(M2FormalExecutionStartAuthorityError, match="signature was rejected"):
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


def test_b_issuer_fails_closed_for_signer_failure_or_owner_role_reuse(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    registration = _registration(fixture, preview)

    with pytest.raises(M2FormalExecutionStartAuthorityError, match="signer failed"):
        _issuer(
            fixture,
            preview,
            registration,
            signer=StartSigner(raises=True),
        ).issue(
            preview_id=preview.preview_id,
            idempotency_key="formal-start-intent-test-v1",
        )
    reused = FormalStartSignerRef(
        signer_id=fixture.authorization.verifier.verifier_id,
        signer_version="1.0.0",
        signer_hash=fixture.authorization.verifier.verifier_hash,
        signature_scheme="test-signature-v1",
        key_id="reused-owner-key",
    )
    with pytest.raises(M2FormalExecutionStartAuthorityError, match="must be separate"):
        _issuer(
            fixture,
            preview,
            registration,
            signer=StartSigner(signer_ref=reused),
        ).issue(
            preview_id=preview.preview_id,
            idempotency_key="formal-start-intent-test-v1",
        )
