# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from hcuopt.domain.errors import Conflict
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
