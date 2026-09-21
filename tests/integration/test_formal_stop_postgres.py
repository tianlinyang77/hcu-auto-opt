# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import os
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

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


def test_stop_is_durable_idempotent_and_blocks_checkpoint(dispatch_case):  # type: ignore[no-untyped-def] # noqa: F811
    dispatcher, intent_id = dispatch_case
    dispatcher.create(intent_id)
    store = PostgresFormalClaimStore(dispatcher, enabled=True)
    claim = store.claim(intent_id, "worker")
    store.assert_active(intent_id, "worker", claim["claim_token"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: store.request_stop(intent_id, requested_by="operator"), range(2))
        )
    assert results[0] == results[1]
    assert results[0]["claim_token"] == claim["claim_token"]
    assert dispatcher.read_status(intent_id).state == "stop_requested"
    fresh = PostgresFormalClaimStore(dispatcher, enabled=True)
    assert fresh.request_stop(intent_id, requested_by="operator") == results[0]
    with pytest.raises(Conflict, match="stop requested"):
        fresh.assert_active(intent_id, "worker", claim["claim_token"])
    with pytest.raises(Conflict, match="recorded audit"):
        fresh.request_stop(intent_id, requested_by="other")
    assert dispatcher.repository.get_formal_start_intent(intent_id).state != "cancelled"
    with dispatcher.repository.connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute("DELETE FROM formal_dispatch_stop_requests")


def test_checkpoint_rejects_wrong_owner_revocation_and_expiry(dispatch_case):  # type: ignore[no-untyped-def] # noqa: F811
    dispatcher, intent_id = dispatch_case
    dispatcher.create(intent_id)
    store = PostgresFormalClaimStore(dispatcher, enabled=True)
    claim = store.claim(intent_id, "worker", ttl_seconds=1)
    for worker, token in [("other", claim["claim_token"]), ("worker", uuid4())]:
        with pytest.raises(Conflict, match="stale"):
            store.assert_active(intent_id, worker, token)
    dispatcher.coordinator.execution_verifier.accepted = False
    with pytest.raises(Conflict):
        store.assert_active(intent_id, "worker", claim["claim_token"])
    dispatcher.coordinator.execution_verifier.accepted = True
    time.sleep(1.05)
    with pytest.raises(Conflict, match="expired"):
        store.assert_active(intent_id, "worker", claim["claim_token"])
    # Stop remains possible after expiry, but does not release or requeue anything.
    store.request_stop(intent_id, requested_by="operator")
    assert store.mark_expired(intent_id)
    assert dispatcher.read_status(intent_id).state == "recovery_required"


def test_stop_rejects_unclaimed_disabled_and_wrong_deployment(dispatch_case):  # type: ignore[no-untyped-def] # noqa: F811
    dispatcher, intent_id = dispatch_case
    dispatcher.create(intent_id)
    store = PostgresFormalClaimStore(dispatcher, enabled=True)
    with pytest.raises(Conflict, match="Unclaimed"):
        store.request_stop(intent_id, requested_by="operator")
    with pytest.raises(Conflict, match="disabled"):
        PostgresFormalClaimStore(dispatcher).request_stop(intent_id, requested_by="operator")
    store.claim(intent_id, "worker")
    dispatcher.coordinator.compiler.service_identity = (
        dispatcher.coordinator.service_identity.model_copy(update={"server_instance_id": uuid4()})
    )
    with pytest.raises(Conflict, match="another deployment"):
        store.request_stop(intent_id, requested_by="operator")


def test_failed_stop_insert_rolls_back_and_leaves_claim_active(dispatch_case, monkeypatch):  # type: ignore[no-untyped-def] # noqa: F811
    from hcuopt.storage import formal_claim

    dispatcher, intent_id = dispatch_case
    dispatcher.create(intent_id)
    store = PostgresFormalClaimStore(dispatcher, enabled=True)
    claim = store.claim(intent_id, "worker")
    original = formal_claim._insert

    def fail_after_write(connection, table, payload):  # type: ignore[no-untyped-def]
        original(connection, table, payload)
        if table == "formal_dispatch_stop_requests":
            raise RuntimeError("injected stop failure")

    monkeypatch.setattr(formal_claim, "_insert", fail_after_write)
    with pytest.raises(RuntimeError, match="injected"):
        store.request_stop(intent_id, requested_by="operator")
    assert dispatcher.read_status(intent_id).state == "claimed"
    store.assert_active(intent_id, "worker", claim["claim_token"])
