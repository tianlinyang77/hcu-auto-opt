# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from pathlib import Path

import pytest

from hcuopt.domain.errors import NotFound, SourceArtifactError
from hcuopt.operator.formal_start_store import (
    MAX_FORMAL_START_AUTHORITY_BYTES,
    DeploymentFormalStartAuthorityStore,
)
from tests.unit.test_formal_evaluation_start_authority import (
    _issuer as _evaluation_issuer,
)
from tests.unit.test_formal_evaluation_start_authority import (
    _registration as _evaluation_registration,
)
from tests.unit.test_formal_execution_start_authority import PreviewStore
from tests.unit.test_formal_execution_start_authority import (
    _issuer as _execution_issuer,
)
from tests.unit.test_formal_execution_start_authority import (
    _registration as _execution_registration,
)
from tests.unit.test_formal_operator_plans import _fixture, _hash
from tests.unit.test_formal_operator_start import (
    START_KEY,
    _coordinator,
    _Repository,
    _request,
)


def _issued_chain(tmp_path: Path):  # type: ignore[no-untyped-def]
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    preview_store = PreviewStore(preview)
    execution = _execution_issuer(
        fixture,
        preview,
        _execution_registration(fixture, preview),
        preview_store=preview_store,
    ).issue(preview_id=preview.preview_id, idempotency_key=START_KEY)
    evaluation = _evaluation_issuer(
        fixture,
        preview,
        _evaluation_registration(fixture, preview, tmp_path / "evidence"),
        preview_store=preview_store,
    ).issue(preview_id=preview.preview_id, idempotency_key=START_KEY)
    store = DeploymentFormalStartAuthorityStore(
        tmp_path / "authority-store",
        preview_store=preview_store,
    )
    return fixture, preview, execution, evaluation, store


def _stored_path(root: Path) -> Path:
    paths = tuple(root.rglob("authority.json"))
    assert len(paths) == 1
    return paths[0]


def test_real_issuers_store_and_coordinator_form_no_hcu_authority_chain(
    tmp_path: Path,
) -> None:
    fixture, preview, execution, evaluation, store = _issued_chain(tmp_path)
    repository = _Repository(fixture.repository)
    coordinator = _coordinator(fixture, store)
    request = _request(
        fixture,
        preview,
        execution.authority_hash,
        evaluation.authority_hash,
    )

    waiting = coordinator.create(request, repository)

    assert waiting.state == "awaiting_authority"
    store.publish_execution_authority(execution)
    store.publish_evaluation_authority(evaluation)
    ready = coordinator.recover(repository)[0]

    assert ready.state == "ready_for_round_creation"
    assert ready.authority_ready is True
    assert ready.round_creation_allowed is False
    assert ready.hcu_accessed is False
    assert ready.automatic_release_allowed is False

    replay = coordinator.create(request, repository)
    assert replay.replayed is True
    assert replay.intent_id == ready.intent_id


def test_authority_store_publish_is_idempotent_and_type_safe(tmp_path: Path) -> None:
    _fixture_value, _preview, execution, evaluation, store = _issued_chain(tmp_path)

    first = store.publish_execution_authority(execution)
    second = store.publish_execution_authority(execution)

    assert first == second == execution
    with pytest.raises(SourceArtifactError, match="another type"):
        store.load_evaluation_authority(execution.authority_hash)
    store.publish_evaluation_authority(evaluation)
    with pytest.raises(SourceArtifactError, match="another type"):
        store.load_execution_authority(evaluation.authority_hash)


def test_authority_store_rejects_missing_invalid_and_mutated_objects(
    tmp_path: Path,
) -> None:
    _fixture_value, _preview, execution, _evaluation, store = _issued_chain(tmp_path)

    with pytest.raises(NotFound):
        store.load_execution_authority(_hash("missing-authority"))
    with pytest.raises(SourceArtifactError, match="invalid SHA256"):
        store.load_execution_authority("sha256:../../outside")
    with pytest.raises(SourceArtifactError, match="invalid SHA256"):
        store.load_execution_authority("a" * 64)

    store.publish_execution_authority(execution)
    path = _stored_path(store.root)
    path.write_bytes(b'{"schema_version":"tampered"}')
    with pytest.raises(SourceArtifactError, match="malformed"):
        store.load_execution_authority(execution.authority_hash)
    with pytest.raises(SourceArtifactError, match="other bytes"):
        store.publish_execution_authority(execution)


def test_authority_store_rejects_oversized_and_linked_objects(tmp_path: Path) -> None:
    _fixture_value, _preview, execution, _evaluation, store = _issued_chain(tmp_path)
    store.publish_execution_authority(execution)
    path = _stored_path(store.root)
    path.write_bytes(b"x" * (MAX_FORMAL_START_AUTHORITY_BYTES + 1))
    with pytest.raises(SourceArtifactError, match="size limit"):
        store.load_execution_authority(execution.authority_hash)

    link_root = tmp_path / "linked-store"
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"{}")
    linked = DeploymentFormalStartAuthorityStore(
        link_root,
        preview_store=store.preview_store,
    )
    expected = (
        link_root
        / "authorities"
        / "sha256"
        / execution.authority_hash.removeprefix("sha256:")[:2]
        / execution.authority_hash.removeprefix("sha256:")[2:]
        / "authority.json"
    )
    expected.parent.mkdir(parents=True)
    try:
        expected.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    with pytest.raises(SourceArtifactError, match="link"):
        linked.load_execution_authority(execution.authority_hash)


def test_issued_authorities_cannot_be_replayed_into_another_intent(
    tmp_path: Path,
) -> None:
    fixture, preview, execution, evaluation, store = _issued_chain(tmp_path)
    store.publish_execution_authority(execution)
    store.publish_evaluation_authority(evaluation)
    repository = _Repository(fixture.repository)

    result = _coordinator(fixture, store).create(
        _request(
            fixture,
            preview,
            execution.authority_hash,
            evaluation.authority_hash,
            idempotency_key="another-formal-start-intent-v1",
        ),
        repository,
    )

    assert result.state == "failed"
    assert result.error_code == "formal_start_authority_invalid"
    assert result.round_creation_allowed is False
    assert result.hcu_accessed is False
    assert result.automatic_release_allowed is False
