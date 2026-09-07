# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from hcuopt.api.app import create_app
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.evaluation.formal_evidence_reporting import (
    FormalEvidenceAcceptanceReportError,
    FormalEvidenceAcceptanceReportService,
)
from hcuopt.storage.formal_evidence_acceptance import (
    _review_from_row,
    _snapshot_from_row,
    formal_evidence_acceptance_snapshot_hash,
)
from hcuopt.storage.repository import PostgresRepository
from tests.unit.test_formal_evidence_acceptance import (
    _acceptance_fixture,
    _ReviewVerifier,
)


class _Repository:
    def __init__(self, snapshot, review) -> None:  # type: ignore[no-untyped-def]
        self.snapshot = snapshot
        self.review = review

    def read_formal_evidence_acceptance_snapshot(
        self, *, round_id, readiness_audit_id
    ):  # type: ignore[no-untyped-def]
        return self.snapshot

    def read_formal_evidence_acceptance_review(
        self, *, round_id, readiness_audit_id
    ):  # type: ignore[no-untyped-def]
        return self.review


def _service():  # type: ignore[no-untyped-def]
    fixture = _acceptance_fixture()
    review = fixture.service.review(
        round_id=fixture.snapshot.round_id,
        readiness_audit_id=fixture.snapshot.readiness_audit_id,
    )
    service = FormalEvidenceAcceptanceReportService(
        _Repository(fixture.snapshot, review),
        _ReviewVerifier(fixture.snapshot.verifier),
    )
    return fixture, review, service


def _application(service, authorizer):  # type: ignore[no-untyped-def]
    return create_app(
        repository=Mock(spec=PostgresRepository),
        formal_evidence_reports=service,
        formal_evidence_read_authorizer=authorizer,
    )


def test_report_service_revalidates_the_persisted_review_signature() -> None:
    fixture, review, service = _service()

    report = service.get(
        round_id=fixture.snapshot.round_id,
        readiness_audit_id=fixture.snapshot.readiness_audit_id,
    )

    assert report.review == review
    assert report.snapshot_hash == review.verification_input_digest
    assert report.owner_window_authorization == "not_granted"
    assert report.hcu_accessed is False
    assert report.automatic_release_allowed is False

    changed_signature = review.signature.model_copy(update={"value": "f" * 64})
    service.repository.review = review.model_copy(  # type: ignore[attr-defined]
        update={"signature": changed_signature}
    )
    with pytest.raises(FormalEvidenceAcceptanceReportError) as rejected:
        service.get(
            round_id=fixture.snapshot.round_id,
            readiness_audit_id=fixture.snapshot.readiness_audit_id,
        )
    assert rejected.value.code == "formal_evidence_acceptance_record_invalid"


def test_persisted_columns_and_payload_tampering_fail_closed() -> None:
    fixture, review, _service_instance = _service()
    snapshot = fixture.snapshot
    snapshot_row = {
        "round_id": snapshot.round_id,
        "readiness_audit_id": snapshot.readiness_audit_id,
        "snapshot_hash": formal_evidence_acceptance_snapshot_hash(snapshot),
        "snapshot": snapshot.model_dump(mode="json"),
    }
    assert _snapshot_from_row(snapshot_row) == snapshot
    changed = dict(snapshot_row)
    changed["snapshot"] = dict(snapshot_row["snapshot"], target_lock_hash="sha256:" + "f" * 64)
    with pytest.raises(SourceArtifactError):
        _snapshot_from_row(changed)
    review_row = {
        "review_id": review.review_id,
        "review_hash": review.review_hash,
        "round_id": review.round_id,
        "readiness_audit_id": review.readiness_audit_id,
        "snapshot_hash": review.verification_input_digest,
        "decision": review.decision,
        "reviewed_at": review.reviewed_at,
        "review": review.model_dump(mode="json"),
    }
    assert _review_from_row(review_row) == review
    for field, value in (
        ("snapshot_hash", "sha256:" + "e" * 64),
        ("reviewed_at", None),
        ("decision", "blocked"),
    ):
        with pytest.raises(SourceArtifactError):
            _review_from_row(dict(review_row, **{field: value}))


def test_formal_acceptance_api_is_authenticated_and_read_only() -> None:
    fixture, review, service = _service()
    application = _application(
        service,
        lambda request, round_id, audit_id: (
            request.headers.get("Authorization") == "Bearer formal-read-token"
            and round_id == fixture.snapshot.round_id
            and audit_id == fixture.snapshot.readiness_audit_id
        ),
    )
    url = (
        f"/v1/operator/formal-rounds/{fixture.snapshot.round_id}/evidence-acceptance"
        f"?readiness_audit_id={fixture.snapshot.readiness_audit_id}"
    )

    with TestClient(application) as client:
        rejected = client.get(url)
        accepted = client.get(
            url,
            headers={"Authorization": "Bearer formal-read-token"},
        )

    assert rejected.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["review"] == review.model_dump(mode="json")
    assert accepted.json()["owner_window_authorization"] == "not_granted"
    methods = application.openapi()["paths"][
        "/v1/operator/formal-rounds/{round_id}/evidence-acceptance"
    ]
    assert set(methods) == {"get"}


def test_formal_acceptance_api_fails_closed_without_auth_or_service() -> None:
    fixture, _review, service = _service()
    url = (
        f"/v1/operator/formal-rounds/{fixture.snapshot.round_id}/evidence-acceptance"
        f"?readiness_audit_id={fixture.snapshot.readiness_audit_id}"
    )

    with TestClient(_application(service, None)) as client:
        no_auth = client.get(url)
    with TestClient(_application(None, lambda _request, _round, _audit: True)) as client:
        no_service = client.get(url)

    assert no_auth.status_code == 503
    assert no_service.status_code == 503


def test_formal_acceptance_api_maps_invalid_persistence_to_422() -> None:
    fixture, review, service = _service()
    service.repository.review = review.model_copy(  # type: ignore[attr-defined]
        update={
            "signature": review.signature.model_copy(update={"value": "0" * 64})
        }
    )
    application = _application(service, lambda _request, _round, _audit: True)
    url = (
        f"/v1/operator/formal-rounds/{fixture.snapshot.round_id}/evidence-acceptance"
        f"?readiness_audit_id={fixture.snapshot.readiness_audit_id}"
    )

    with TestClient(application) as client:
        response = client.get(url)

    assert response.status_code == 422
    assert response.json() == {
        "code": "formal_evidence_acceptance_record_invalid",
        "message": "Persisted Formal acceptance evidence failed closed validation",
        "retryable": False,
    }
