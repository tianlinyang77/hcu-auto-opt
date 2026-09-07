# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from hcuopt.api.app import create_app
from hcuopt.domain.errors import Conflict
from hcuopt.evaluation.formal_evidence_reporting import FormalEvidenceAcceptanceReportService
from hcuopt.storage.formal_evidence_acceptance import DeploymentFormalEvidenceAcceptanceRegistry
from hcuopt.storage.repository import PostgresRepository
from tests.unit.test_formal_evidence_acceptance import (
    _acceptance_fixture,
    _ReviewVerifier,
)

try:
    import psycopg
except ImportError:  # pragma: no cover - package dependency in normal installs
    psycopg = None


DATABASE_URL = os.getenv("HCUOPT_DATABASE_URL")


@unittest.skipUnless(DATABASE_URL and psycopg, "requires PostgreSQL and psycopg")
@pytest.mark.postgres
class FormalEvidenceAcceptancePostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        assert DATABASE_URL is not None
        assert psycopg is not None
        self.repository = PostgresRepository(DATABASE_URL)
        self.repository.migrate()
        self.connection = psycopg.connect(DATABASE_URL)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                TRUNCATE formal_evidence_acceptance_reviews,
                         formal_evidence_acceptance_snapshots
                """
            )
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()

    def test_recursive_acceptance_survives_registry_and_api_restart(self) -> None:
        for label, holdout, wrong_owner in (
            ("zero", False, False),
            ("holdout", True, False),
            ("rejected", False, True),
        ):
            with self.subTest(terminal=label):
                fixture = _acceptance_fixture(
                    holdout=holdout, wrong_owner_identity=wrong_owner
                )
                snapshot = fixture.snapshot.model_copy(
                    update={"readiness_audit_id": f"formal-roundtrip-{label}"}
                )
                verifier = _ReviewVerifier(snapshot.verifier)
                registry = DeploymentFormalEvidenceAcceptanceRegistry(
                    self.repository, signature_verifier=verifier
                )
                registry.register(snapshot)
                # The real acceptance service must resolve the DB Snapshot itself.
                fixture.service.snapshot_reader = registry
                review = fixture.service.review(
                    round_id=snapshot.round_id,
                    readiness_audit_id=snapshot.readiness_audit_id,
                )
                stored, created = registry.publish_review(review)
                self.assertTrue(created)
                self.assertEqual(stored, review)

                restarted = PostgresRepository(DATABASE_URL)
                fresh_registry = DeploymentFormalEvidenceAcceptanceRegistry(
                    restarted, signature_verifier=_ReviewVerifier(snapshot.verifier)
                )
                self.assertEqual(
                    fresh_registry.read_review(
                        round_id=snapshot.round_id,
                        readiness_audit_id=snapshot.readiness_audit_id,
                    ),
                    review,
                )
                reports = FormalEvidenceAcceptanceReportService(
                    restarted, _ReviewVerifier(snapshot.verifier)
                )
                application = create_app(
                    repository=restarted,
                    formal_evidence_reports=reports,
                    formal_evidence_read_authorizer=lambda request, round_id, audit_id,
                    expected=snapshot: (
                        request.headers.get("Authorization") == "Bearer test-only-read"
                        and round_id == expected.round_id
                        and audit_id == expected.readiness_audit_id
                    ),
                )
                url = f"/v1/operator/formal-rounds/{snapshot.round_id}/evidence-acceptance"
                with TestClient(application) as client:
                    denied = client.get(
                        url, params={"readiness_audit_id": snapshot.readiness_audit_id}
                    )
                    response = client.get(
                        url,
                        params={"readiness_audit_id": snapshot.readiness_audit_id},
                        headers={"Authorization": "Bearer test-only-read"},
                    )
                self.assertEqual(denied.status_code, 403)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["review"], review.model_dump(mode="json"))
                self.assertEqual(
                    review.decision,
                    "blocked" if wrong_owner else "accepted_for_formal_window",
                )
                self.assertEqual(bool(review.blocker_codes), wrong_owner)
                self.assertFalse(response.json()["hcu_accessed"])
                self.assertFalse(response.json()["automatic_release_allowed"])
                self.assertEqual(response.json()["owner_window_authorization"], "not_granted")

    def test_snapshot_and_signed_review_are_concurrent_idempotent_and_immutable(self) -> None:
        fixture = _acceptance_fixture()
        snapshot = fixture.snapshot
        review = fixture.service.review(
            round_id=snapshot.round_id,
            readiness_audit_id=snapshot.readiness_audit_id,
        )
        signature_verifier = _ReviewVerifier(snapshot.verifier)
        barrier = Barrier(2)

        def register_once():  # type: ignore[no-untyped-def]
            barrier.wait(timeout=10)
            return PostgresRepository(
                DATABASE_URL
            ).register_formal_evidence_acceptance_snapshot(snapshot)

        with ThreadPoolExecutor(max_workers=2) as executor:
            registered = tuple(executor.map(lambda _index: register_once(), range(2)))

        self.assertEqual({item[1] for item in registered}, {False, True})
        self.assertTrue(all(item[0] == snapshot for item in registered))

        barrier = Barrier(2)

        def publish_once():  # type: ignore[no-untyped-def]
            barrier.wait(timeout=10)
            return PostgresRepository(
                DATABASE_URL
            ).publish_formal_evidence_acceptance_review(
                review,
                signature_verifier=signature_verifier,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            published = tuple(executor.map(lambda _index: publish_once(), range(2)))

        self.assertEqual({item[1] for item in published}, {False, True})
        self.assertTrue(all(item[0] == review for item in published))
        self.assertEqual(
            self.repository.read_formal_evidence_acceptance_snapshot(
                round_id=snapshot.round_id,
                readiness_audit_id=snapshot.readiness_audit_id,
            ),
            snapshot,
        )
        self.assertEqual(
            self.repository.read_formal_evidence_acceptance_review(
                round_id=snapshot.round_id,
                readiness_audit_id=snapshot.readiness_audit_id,
            ),
            review,
        )

        with self.connection.cursor() as cursor:
            with self.assertRaises(psycopg.errors.RaiseException):
                cursor.execute(
                    """
                    UPDATE formal_evidence_acceptance_reviews
                    SET decision = 'blocked'
                    WHERE review_id = %s
                    """,
                    (review.review_id,),
                )
        self.connection.rollback()

    def test_snapshot_identity_cannot_be_rebound(self) -> None:
        fixture = _acceptance_fixture()
        snapshot = fixture.snapshot
        self.repository.register_formal_evidence_acceptance_snapshot(snapshot)

        with self.assertRaises(Conflict):
            self.repository.register_formal_evidence_acceptance_snapshot(
                snapshot.model_copy(update={"target_lock_hash": "sha256:" + "d" * 64})
            )
