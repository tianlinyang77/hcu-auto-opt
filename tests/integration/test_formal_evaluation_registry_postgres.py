# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest

from hcuopt.domain.errors import Conflict, NotFound, SourceArtifactError
from hcuopt.evaluation.m2_formal_registration import DeploymentFormalEvaluationStartRegistry
from hcuopt.storage.repository import PostgresRepository
from tests.unit.test_formal_evaluation_start_authority import _issuer, _registration
from tests.unit.test_formal_operator_plans import _fixture, _hash

DATABASE_URL = os.getenv("HCUOPT_DATABASE_URL")
pytestmark = [pytest.mark.postgres, pytest.mark.skipif(not DATABASE_URL, reason="needs PostgreSQL")]


def test_d_issuer_consumes_restarted_registry_and_rejects_rebinding(tmp_path: Path) -> None:
    repo = PostgresRepository(DATABASE_URL)
    repo.migrate()
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    registration = _registration(fixture, preview, tmp_path)
    registry = DeploymentFormalEvaluationStartRegistry(repo.connection)
    key = dict(
        preview_id=preview.preview_id,
        formal_authorization_hash=registration.formal_authorization_hash,
        resolved_plan_hash=registration.resolved_plan_hash,
    )
    with pytest.raises(NotFound):
        registry.load_registration(**key)
    barrier = Barrier(2)

    def publish():  # type: ignore[no-untyped-def]
        barrier.wait(timeout=10)
        return registry.publish_registration(registration)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: publish(), range(2)))
    assert results == (registration, registration)
    restarted = DeploymentFormalEvaluationStartRegistry(PostgresRepository(DATABASE_URL).connection)
    assert restarted.load_registration(**key) == registration
    issued = _issuer(
        fixture, preview, registration, evaluation_registry=restarted
    ).issue(preview_id=preview.preview_id, idempotency_key="formal-registry-integration-v1")
    assert issued.holdout_plan_commitment == registration.holdout_plan_commitment
    assert issued.independent_verifier == registration.independent_verifier
    assert issued.automatic_release_allowed is False

    for changed in (
        registration.model_copy(update={"registration_id": uuid4()}),
        registration.model_copy(update={"holdout_plan_commitment": _hash("drift")}),
    ):
        with pytest.raises(Conflict):
            restarted.publish_registration(changed)
    with pytest.raises(SourceArtifactError):
        restarted.load_registration(**(key | {"resolved_plan_hash": _hash("wrong-plan")}))
    with repo.connection() as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "DELETE FROM formal_evaluation_start_registrations WHERE registration_id = %s",
                (registration.registration_id,),
            )
        connection.rollback()
