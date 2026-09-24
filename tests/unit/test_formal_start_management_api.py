# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hcuopt.api.app import create_app
from hcuopt.api.formal_start_management import (
    FormalIntentCapability,
    FormalIntentSubmission,
    FormalStartManagement,
)
from tests.unit.test_formal_operator_plans import NOW, _fixture
from tests.unit.test_formal_operator_start import (
    _authorities,
    _coordinator,
    _ObjectStore,
    _Repository,
    _request,
)

TOKEN = "test-only-formal-create-capability-not-a-production-secret"


def setup_management(tmp_path: Path):  # type: ignore[no-untyped-def]
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    execution, evaluation = _authorities(fixture, preview, tmp_path)
    store = _ObjectStore(preview, execution, evaluation)
    repository = _Repository(fixture.repository)
    signed = _request(fixture, preview, execution.authority_hash, evaluation.authority_hash)
    management = FormalStartManagement(
        coordinator=_coordinator(fixture, store),
        capabilities=(
            FormalIntentCapability(sha256(TOKEN.encode()).hexdigest(), signed.actor_assertion),
        ),
    )
    payload = signed.model_dump(mode="json", exclude={"actor_assertion"})
    return management, repository, payload


def client_for(management, repository):  # type: ignore[no-untyped-def]
    return TestClient(create_app(repository=repository, formal_start_management=management))


def test_opt_in_create_replay_and_restart_never_dispatch(tmp_path: Path) -> None:
    management, repository, payload = setup_management(tmp_path)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with client_for(management, repository) as client:
        first = client.post("/v1/operator/formal-start-intents", json=payload, headers=headers)
    assert first.status_code == 200, first.text
    # New app instance, same persisted capability binding and repository.
    with client_for(replace(management), repository) as client:
        replay = client.post("/v1/operator/formal-start-intents", json=payload, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert first.json()["intent_id"] == replay.json()["intent_id"]
    assert replay.json()["state"] == "ready_for_round_creation"
    assert replay.headers["cache-control"] == "no-store"
    for field in ("round_creation_allowed", "hcu_accessed", "automatic_release_allowed"):
        assert replay.json()[field] is False
    # This repository has no Round/Job/Lease creation methods: any dispatch fails.
    assert len(repository.intents) == 1


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Cookie": f"token={TOKEN}"}]
)
def test_unauthorized_requests_do_not_write(tmp_path: Path, headers: dict) -> None:
    management, repository, payload = setup_management(tmp_path)
    with client_for(management, repository) as client:
        result = client.post("/v1/operator/formal-start-intents", json=payload, headers=headers)
    assert result.status_code == 403
    assert not repository.intents


@pytest.mark.parametrize(
    "field,value",
    [
        ("idempotency_key", "different-start-key"),
        ("resolved_plan_hash", "sha256:" + "f" * 64),
        ("execution_authority_hash", "sha256:" + "f" * 64),
        ("evaluation_authority_hash", "sha256:" + "f" * 64),
    ],
)
def test_capability_cannot_authorize_changed_request(
    tmp_path: Path, field: str, value: str
) -> None:
    management, repository, payload = setup_management(tmp_path)
    payload[field] = value
    with client_for(management, repository) as client:
        result = client.post(
            "/v1/operator/formal-start-intents",
            json=payload,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert result.status_code == 403
    assert not repository.intents


@pytest.mark.parametrize("field", ["actor_id", "actor_assertion", "automatic_release_allowed"])
def test_client_identity_and_authorization_injection_rejected(tmp_path: Path, field: str) -> None:
    management, repository, payload = setup_management(tmp_path)
    payload[field] = "injected"
    with client_for(management, repository) as client:
        result = client.post(
            "/v1/operator/formal-start-intents",
            json=payload,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert result.status_code == 422
    assert not repository.intents


def test_expired_assertion_does_not_write(tmp_path: Path) -> None:
    management, repository, payload = setup_management(tmp_path)
    management.coordinator.clock = lambda: NOW + timedelta(hours=1)
    with client_for(management, repository) as client:
        result = client.post(
            "/v1/operator/formal-start-intents",
            json=payload,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert result.status_code == 409
    assert not repository.intents


def test_rejected_signature_does_not_write(tmp_path: Path) -> None:
    management, repository, payload = setup_management(tmp_path)
    management.coordinator.actor_verifier.accepted = False
    with client_for(management, repository) as client:
        result = client.post(
            "/v1/operator/formal-start-intents",
            json=payload,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert result.status_code == 409
    assert not repository.intents


def test_revoking_capability_blocks_replay(tmp_path: Path) -> None:
    management, repository, payload = setup_management(tmp_path)
    with client_for(management, repository) as client:
        accepted = client.post(
            "/v1/operator/formal-start-intents",
            json=payload,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert accepted.status_code == 200
    before = repository.get_formal_start_intent(next(iter(repository.intents))).model_dump(
        mode="json"
    )
    with client_for(replace(management, capabilities=()), repository) as client:
        denied = client.post(
            "/v1/operator/formal-start-intents",
            json=payload,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert denied.status_code == 403
    assert (
        repository.get_formal_start_intent(next(iter(repository.intents))).model_dump(mode="json")
        == before
    )


def test_default_app_has_no_formal_create_route() -> None:
    assert "/v1/operator/formal-start-intents" not in create_app().openapi()["paths"]


def test_duplicate_capability_binding_is_rejected(tmp_path: Path) -> None:
    management, _, _ = setup_management(tmp_path)
    with pytest.raises(ValueError, match="unique"):
        replace(management, capabilities=management.capabilities * 2)


def test_capability_file_preserves_binding_across_loads(tmp_path: Path) -> None:
    management, _, _ = setup_management(tmp_path)
    path = tmp_path / "capabilities.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "formal-intent-capabilities-v1",
                "capabilities": [
                    {
                        "token_sha256": management.capabilities[0].token_sha256,
                        "assertion": management.capabilities[0].assertion.model_dump(mode="json"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    first = FormalStartManagement.from_file(
        management.coordinator, deployment_root=tmp_path, path=path
    )
    second = FormalStartManagement.from_file(
        management.coordinator, deployment_root=tmp_path, path=path
    )
    assert first.capabilities == second.capabilities == management.capabilities
    assert TOKEN not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "content",
    [
        "not-json-sensitive-marker",
        '{"schema_version":"wrong","capabilities":[]}',
        '{"schema_version":"formal-intent-capabilities-v1","capabilities":[],"token":"secret"}',
        pytest.param("x" * (512 * 1024 + 1), id="oversized"),
    ],
)
def test_invalid_capability_file_fails_without_echoing_content(
    tmp_path: Path, content: str
) -> None:
    management, _, _ = setup_management(tmp_path)
    path = tmp_path / "invalid.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        FormalStartManagement.from_file(management.coordinator, deployment_root=tmp_path, path=path)
    assert str(caught.value) == "Formal capability configuration is unavailable or invalid"


def test_capability_file_cannot_escape_root(tmp_path: Path) -> None:
    management, _, _ = setup_management(tmp_path)
    root = tmp_path / "protected"
    root.mkdir()
    path = tmp_path / "outside.json"
    path.write_text(
        '{"schema_version":"formal-intent-capabilities-v1","capabilities":[]}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unavailable or invalid"):
        FormalStartManagement.from_file(management.coordinator, deployment_root=root, path=path)


def test_preparation_returns_only_bound_submission_without_writes(tmp_path: Path) -> None:
    management, repository, payload = setup_management(tmp_path)
    management = replace(
        management,
        capabilities=(
            replace(
                management.capabilities[0],
                submission=FormalIntentSubmission.model_validate(payload),
            ),
        ),
    )
    with client_for(management, repository) as client:
        assert client.get("/v1/operator/formal-start-submission").status_code == 403
        loaded = client.get(
            "/v1/operator/formal-start-submission", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert loaded.status_code == 200
        assert loaded.json() == payload
        assert loaded.headers["cache-control"] == "no-store"
        management.coordinator.clock = lambda: NOW + timedelta(hours=1)
        assert (
            client.get(
                "/v1/operator/formal-start-submission", headers={"Authorization": f"Bearer {TOKEN}"}
            ).status_code
            == 403
        )
    assert not repository.intents
    with pytest.raises(ValueError, match="signed scope"):
        replace(
            management.capabilities[0],
            submission=FormalIntentSubmission.model_validate(
                {**payload, "idempotency_key": "different-key"}
            ),
        )
