# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from hcuopt.contracts.m2_formal_start_v1 import (
    FormalStartCandidateBinding,
    FormalStartIntentView,
)
from hcuopt.contracts.operator_v1 import OperatorServiceIdentity
from hcuopt.domain.errors import Conflict
from hcuopt.storage.repository import PostgresRepository

try:
    import psycopg
except ImportError:  # pragma: no cover - package dependency in normal installs
    psycopg = None


DATABASE_URL = os.getenv("HCUOPT_DATABASE_URL")
NOW = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _identity() -> OperatorServiceIdentity:
    return OperatorServiceIdentity(
        source_commit="a" * 40,
        control_contract_version="v1",
        profile_catalog_hash=_hash("1"),
        server_instance_id=UUID("00000000-0000-0000-0000-000000000087"),
    )


def _intent(key: str = "formal-start-postgres-v1") -> FormalStartIntentView:
    intent_id = uuid5(NAMESPACE_URL, f"hcuopt:test:formal-start:{key}")
    round_id = uuid5(intent_id, "round")
    bindings = tuple(
        FormalStartCandidateBinding(
            ordinal=ordinal,
            candidate_id=uuid5(intent_id, f"candidate:{ordinal}"),
            round_candidate_id=uuid5(round_id, f"member:{ordinal}"),
            candidate_input_digest=_hash(str(ordinal + 2)),
        )
        for ordinal in range(2)
    )
    return FormalStartIntentView(
        intent_id=intent_id,
        preview_id=uuid5(intent_id, "preview"),
        resolved_plan_hash=_hash("2"),
        formal_authorization_hash=_hash("3"),
        execution_authority_hash=_hash("4"),
        evaluation_authority_hash=_hash("5"),
        request_digest=_hash("6"),
        actor_id="formal-postgres-operator",
        actor_assertion_hash=_hash("7"),
        actor_signer_id="formal-postgres-authenticator",
        actor_signer_hash=_hash("8"),
        idempotency_key=key,
        task_id=uuid5(intent_id, "task"),
        round_id=round_id,
        candidate_bindings=bindings,
        state="awaiting_authority",
        blocker_codes=(
            "formal_evaluation_start_authority_unavailable",
            "formal_execution_start_authority_unavailable",
        ),
        service_identity=_identity(),
        authority_reconcile_count=0,
        version=1,
        created_at=NOW,
        updated_at=NOW,
        authority_ready=False,
    )


@unittest.skipUnless(DATABASE_URL and psycopg, "requires PostgreSQL and psycopg")
@pytest.mark.postgres
class FormalStartIntentPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        assert DATABASE_URL is not None
        assert psycopg is not None
        self.repository = PostgresRepository(DATABASE_URL)
        self.repository.migrate()
        self.connection = psycopg.connect(DATABASE_URL)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                TRUNCATE formal_operator_start_intent_events,
                         formal_operator_start_intents
                """
            )
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()

    def test_concurrent_create_and_reconcile_are_idempotent_and_audited(self) -> None:
        intent = _intent()
        barrier = Barrier(2)

        def create_once():  # type: ignore[no-untyped-def]
            barrier.wait(timeout=10)
            return PostgresRepository(DATABASE_URL).create_formal_start_intent(intent)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = tuple(executor.map(lambda _index: create_once(), range(2)))

        self.assertEqual({first[1], second[1]}, {False, True})
        self.assertEqual(first[0].intent_id, second[0].intent_id)
        restarted_repository = PostgresRepository(DATABASE_URL)
        self.assertIn(
            intent.intent_id,
            restarted_repository.list_recoverable_formal_start_intent_ids(100),
        )

        barrier = Barrier(2)

        def reconcile_once():  # type: ignore[no-untyped-def]
            barrier.wait(timeout=10)
            return PostgresRepository(DATABASE_URL).record_formal_start_reconciliation(
                intent.intent_id,
                state="ready_for_round_creation",
                blocker_codes=(),
                checked_at=NOW,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            reconciled = tuple(executor.map(lambda _index: reconcile_once(), range(2)))

        self.assertTrue(all(item.authority_ready for item in reconciled))
        stored = self.repository.get_formal_start_intent(intent.intent_id)
        self.assertEqual(stored.state, "ready_for_round_creation")
        self.assertEqual(stored.authority_reconcile_count, 2)
        self.assertFalse(stored.round_creation_allowed)
        self.assertFalse(stored.hcu_accessed)
        with self.connection.cursor() as cursor:
            event_count = cursor.execute(
                """
                SELECT count(*) FROM formal_operator_start_intent_events
                WHERE intent_id = %s
                """,
                (intent.intent_id,),
            ).fetchone()[0]
            round_count = cursor.execute(
                "SELECT count(*) FROM search_rounds WHERE round_id = %s",
                (intent.round_id,),
            ).fetchone()[0]
        self.assertEqual(event_count, 3)
        self.assertEqual(round_count, 0)

    def test_idempotency_conflict_cancel_and_append_only_events(self) -> None:
        intent = _intent()
        stored, created = self.repository.create_formal_start_intent(intent)
        self.assertTrue(created)
        changed = intent.model_copy(update={"resolved_plan_hash": _hash("9")})
        with self.assertRaises(Conflict):
            self.repository.create_formal_start_intent(changed)

        cancelled = self.repository.cancel_formal_start_intent(
            stored.intent_id,
            cancelled_at=NOW,
        )
        self.assertEqual(cancelled.state, "cancelled")
        self.assertFalse(cancelled.authority_ready)
        with self.connection.cursor() as cursor:
            with self.assertRaises(psycopg.errors.RaiseException):
                cursor.execute(
                    """
                    DELETE FROM formal_operator_start_intent_events
                    WHERE intent_id = %s
                    """,
                    (stored.intent_id,),
                )
        self.connection.rollback()
