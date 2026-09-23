# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import os
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from hcuopt.domain.errors import Conflict
from hcuopt.storage.formal_claim import PostgresFormalClaimStore
from tests.integration.test_formal_dispatch_postgres import dispatch_case  # noqa: F401
from tests.integration.test_formal_start_management_postgres import isolated_dsn  # noqa: F401

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.getenv("HCUOPT_DATABASE_URL"), reason="requires PostgreSQL"),
]


def test_only_one_claim_and_timeout_never_requeues(dispatch_case):  # type: ignore[no-untyped-def] # noqa: F811
    dispatcher, intent_id = dispatch_case
    dispatcher.create(intent_id)
    store = PostgresFormalClaimStore(dispatcher, enabled=True)

    def attempt(worker):  # type: ignore[no-untyped-def]
        try:
            return store.claim(intent_id, worker, ttl_seconds=1)
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["worker-a", "worker-b"]))
    assert sum(result is not None for result in results) == 1
    assert dispatcher.read_status(intent_id).state == "claimed"
    time.sleep(1.05)
    assert store.mark_expired(intent_id)
    assert not store.mark_expired(intent_id)
    assert dispatcher.read_status(intent_id).state == "recovery_required"
    with pytest.raises(Conflict, match="already claimed"):
        PostgresFormalClaimStore(dispatcher, enabled=True).claim(intent_id, "replacement")
    with dispatcher.repository.connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0
        assert (
            connection.execute("SELECT count(*) AS n FROM formal_dispatch_claim_events").fetchone()[
                "n"
            ]
            == 2
        )
        with pytest.raises(psycopg.errors.RaiseException, match="mutation"):
            connection.execute("UPDATE formal_dispatch_claims SET worker_id = 'intruder'")


def test_claim_cancel_race_is_serialized(dispatch_case):  # type: ignore[no-untyped-def] # noqa: F811
    dispatcher, intent_id = dispatch_case
    dispatcher.create(intent_id)
    store = PostgresFormalClaimStore(dispatcher, enabled=True)

    def claim():  # type: ignore[no-untyped-def]
        try:
            store.claim(intent_id, "worker")
            return "claimed"
        except Conflict:
            return "claim_rejected"

    def cancel():  # type: ignore[no-untyped-def]
        with dispatcher.repository.connection() as connection:
            now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        try:
            dispatcher.repository.cancel_formal_start_intent(intent_id, cancelled_at=now)
            return "cancelled"
        except psycopg.errors.RaiseException:
            return "cancel_rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(claim), pool.submit(cancel)
        outcomes = {a.result(), b.result()}
    assert outcomes in ({"claimed", "cancel_rejected"}, {"cancelled", "claim_rejected"})
    assert dispatcher.read_status(intent_id).state in {"claimed", "cancelled"}


def test_claim_disabled_and_invalid_ttl(dispatch_case):  # type: ignore[no-untyped-def] # noqa: F811
    dispatcher, intent_id = dispatch_case
    dispatcher.create(intent_id)
    with pytest.raises(Conflict, match="disabled"):
        PostgresFormalClaimStore(dispatcher).claim(intent_id, "worker")
    store = PostgresFormalClaimStore(dispatcher, enabled=True)
    for ttl in [0, 301, True]:
        with pytest.raises(ValueError, match="TTL"):
            store.claim(intent_id, "worker", ttl_seconds=ttl)
    assert dispatcher.read_status(intent_id).state == "queued"


def test_claim_rechecks_signature_and_rolls_back_partial_write(dispatch_case, monkeypatch):  # type: ignore[no-untyped-def] # noqa: F811
    from hcuopt.storage import formal_claim

    dispatcher, intent_id = dispatch_case
    dispatcher.create(intent_id)
    store = PostgresFormalClaimStore(dispatcher, enabled=True)
    dispatcher.coordinator.execution_verifier.accepted = False
    with pytest.raises(Conflict):
        store.claim(intent_id, "worker")
    dispatcher.coordinator.execution_verifier.accepted = True
    original = formal_claim._insert

    def fail_event(connection, table, payload):  # type: ignore[no-untyped-def]
        if table == "formal_dispatch_claim_events":
            raise RuntimeError("injected event failure")
        original(connection, table, payload)

    monkeypatch.setattr(formal_claim, "_insert", fail_event)
    with pytest.raises(RuntimeError, match="injected"):
        store.claim(intent_id, "worker")
    assert dispatcher.read_status(intent_id).state == "queued"
    with dispatcher.repository.connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM formal_dispatch_claims").fetchone()[
            "n"
        ] == 0
