# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from types import SimpleNamespace
from uuid import uuid4

import pytest

from hcuopt.domain.errors import Conflict
from hcuopt.workers.formal_search_consumer import (
    FormalSearchBatchMaterials,
    FormalSearchConsumer,
)
from tests.unit.test_formal_search import setup_case
from tests.unit.test_m2_formal_finalizer import NOW


def setup():
    args, _, repository = setup_case()
    claims = SimpleNamespace(checks=[])
    claims.assert_active = lambda *items: claims.checks.append(items)
    reader = SimpleNamespace(
        load=lambda: FormalSearchBatchMaterials(
            round_authority=args["round_authority"],
            context=args["context"],
            members=args["members"],
            references=args["references"],
        )
    )
    consumer = FormalSearchConsumer(
        claims,
        intent_id=uuid4(),
        worker_id="test-d",
        claim_token=uuid4(),
        material_reader=reader,
        verifier=args["verifier"],
        publisher=args["publisher"],
        repository=repository,
        enabled=True,
    )
    return consumer, claims, repository


def test_claim_bound_search_closes_one_durable_batch():
    consumer, claims, repository = setup()
    record = consumer.execute_current_once(
        closed_by="test-d",
        closed_at=NOW,
        idempotency_key="search-consumer-test",
    )
    assert repository.records == [record]
    assert len(claims.checks) == 2


def test_disabled_search_never_reads_or_persists():
    consumer, claims, repository = setup()
    consumer.enabled = False
    with pytest.raises(Conflict, match="disabled"):
        consumer.execute_current_once(
            closed_by="test-d",
            closed_at=NOW,
            idempotency_key="search-consumer-test",
        )
    assert not claims.checks
    assert not repository.records


def test_claim_is_checked_before_reading_batch():
    consumer, claims, _ = setup()
    consumer.claims.assert_active = lambda *items: (_ for _ in ()).throw(Conflict("stopped"))
    with pytest.raises(Conflict, match="stopped"):
        consumer.execute_current_once(
            closed_by="test-d",
            closed_at=NOW,
            idempotency_key="search-consumer-test",
        )
