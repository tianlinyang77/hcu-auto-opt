# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from hcuopt.domain.errors import NotFound, SourceArtifactError
from hcuopt.measurement.m2_formal_profile_registry import (
    MAX_FORMAL_EXECUTION_PROFILE_REGISTRATION_BYTES,
    DeploymentM2FormalExecutionProfileRegistry,
    m2_formal_execution_profile_registration_hash,
)
from hcuopt.measurement.m2_formal_start_authority import (
    M2FormalExecutionStartAuthorityIssuer,
)
from tests.unit.test_formal_execution_start_authority import (
    AuthorizationVerifier,
    PreviewStore,
    StartSigner,
    _registration,
)
from tests.unit.test_formal_operator_plans import NOW, _fixture, _hash


def _prepared(tmp_path: Path):  # type: ignore[no-untyped-def]
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    registration = _registration(fixture, preview)
    registry = DeploymentM2FormalExecutionProfileRegistry(tmp_path / "profile-registry")
    return fixture, preview, registration, registry


def _registration_path(root: Path) -> Path:
    paths = tuple(root.rglob("registration.json"))
    assert len(paths) == 1
    return paths[0]


def test_durable_profile_registry_feeds_b_issuer_without_hcu(tmp_path: Path) -> None:
    fixture, preview, registration, registry = _prepared(tmp_path)

    first = registry.publish_registration(registration)
    replay = registry.publish_registration(registration)
    issued = M2FormalExecutionStartAuthorityIssuer(
        preview_store=PreviewStore(preview),
        profile_registry=registry,
        authorization_verifier=AuthorizationVerifier(fixture.authorization.verifier),
        signer=StartSigner(),
        clock=lambda: NOW,
    ).issue(
        preview_id=preview.preview_id,
        idempotency_key="formal-profile-registry-start-v1",
    )

    assert first == replay == registration
    assert issued.adapter_profile_hash == registration.profile.profile_hash
    assert issued.lease_policy_hash == registration.lease_policy_hash
    assert issued.fencing_policy_hash == registration.fencing_policy_hash
    assert issued.cleanup_policy_hash == registration.cleanup_policy_hash
    assert issued.synthetic is False
    assert issued.automatic_release_allowed is False


def test_profile_registry_rejects_missing_invalid_and_conflicting_bindings(
    tmp_path: Path,
) -> None:
    _fixture_value, _preview, registration, registry = _prepared(tmp_path)

    with pytest.raises(NotFound):
        registry.load_registration(
            adapter_profile_id=registration.profile.profile_id,
            formal_authorization_hash=registration.formal_authorization_hash,
        )
    with pytest.raises(SourceArtifactError, match="invalid Profile ID"):
        registry.load_registration(
            adapter_profile_id="../escaped",
            formal_authorization_hash=registration.formal_authorization_hash,
        )
    with pytest.raises(SourceArtifactError, match="invalid authorization Hash"):
        registry.load_registration(
            adapter_profile_id=registration.profile.profile_id,
            formal_authorization_hash="sha256:../../escaped",
        )

    registry.publish_registration(registration)
    same_key = registration.model_copy(
        update={
            "registration_id": uuid4(),
            "lease_policy_hash": _hash("another-lease-policy"),
        }
    )
    with pytest.raises(SourceArtifactError, match="identity already has other bytes"):
        registry.publish_registration(same_key)
    same_id = registration.model_copy(
        update={"fencing_policy_hash": _hash("another-fencing-policy")}
    )
    with pytest.raises(SourceArtifactError, match="identity already has other bytes"):
        registry.publish_registration(same_id)

    malformed = registration.model_copy(update={"automatic_release_allowed": True})
    with pytest.raises(SourceArtifactError, match="malformed"):
        DeploymentM2FormalExecutionProfileRegistry(
            tmp_path / "malformed-registry"
        ).publish_registration(malformed)


def test_profile_registry_detects_content_and_binding_tampering(tmp_path: Path) -> None:
    _fixture_value, _preview, registration, registry = _prepared(tmp_path)
    registry.publish_registration(registration)
    registration_path = _registration_path(registry.root)
    original = registration_path.read_bytes()

    registration_path.write_bytes(b'{"schema_version":"tampered"}')
    with pytest.raises(SourceArtifactError, match="content is malformed"):
        registry.load_registration(
            adapter_profile_id=registration.profile.profile_id,
            formal_authorization_hash=registration.formal_authorization_hash,
        )

    registration_path.write_bytes(original.rstrip(b"\n") + b" \n")
    with pytest.raises(SourceArtifactError, match="content differs from its binding"):
        registry.load_registration(
            adapter_profile_id=registration.profile.profile_id,
            formal_authorization_hash=registration.formal_authorization_hash,
        )

    registration_path.write_bytes(original)

    binding = next((registry.root / "bindings" / "authorization").rglob("*.json"))
    original_binding = binding.read_bytes()
    binding.write_bytes(b"{}")
    with pytest.raises(SourceArtifactError, match="binding changed"):
        registry.load_registration(
            adapter_profile_id=registration.profile.profile_id,
            formal_authorization_hash=registration.formal_authorization_hash,
        )

    binding.write_bytes(original_binding)
    id_binding = (
        registry.root
        / "bindings"
        / "registration-id"
        / f"{registration.registration_id}.json"
    )
    id_binding.write_bytes(b"{}")
    with pytest.raises(SourceArtifactError, match="ID was rebound"):
        registry.load_registration(
            adapter_profile_id=registration.profile.profile_id,
            formal_authorization_hash=registration.formal_authorization_hash,
        )


def test_profile_registry_concurrent_key_binding_has_one_winner(tmp_path: Path) -> None:
    _fixture_value, _preview, registration, registry = _prepared(tmp_path)
    competitor = registration.model_copy(
        update={
            "registration_id": uuid4(),
            "cleanup_policy_hash": _hash("competing-cleanup-policy"),
        }
    )
    barrier = Barrier(2)

    def publish(value):  # type: ignore[no-untyped-def]
        barrier.wait(timeout=5)
        try:
            return registry.publish_registration(value)
        except SourceArtifactError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(publish, (registration, competitor)))

    winners = tuple(item for item in results if item is not None)
    assert len(winners) == 1
    assert registry.load_registration(
        adapter_profile_id=registration.profile.profile_id,
        formal_authorization_hash=registration.formal_authorization_hash,
    ) == winners[0]


def test_profile_registry_rejects_oversized_or_unbound_content(tmp_path: Path) -> None:
    _fixture_value, _preview, registration, registry = _prepared(tmp_path)
    registry.publish_registration(registration)
    path = _registration_path(registry.root)
    path.write_bytes(b"x" * (MAX_FORMAL_EXECUTION_PROFILE_REGISTRATION_BYTES + 1))

    with pytest.raises(SourceArtifactError, match="size limit"):
        registry.load_registration(
            adapter_profile_id=registration.profile.profile_id,
            formal_authorization_hash=registration.formal_authorization_hash,
        )

    assert m2_formal_execution_profile_registration_hash(registration).startswith("sha256:")
