# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from hcuopt.adapters.m1_verification import M1KernelCorrectnessWorkerAdapter
from hcuopt.domain.errors import Conflict
from hcuopt.workers.formal_correctness_consumer import FormalCorrectnessConsumer
from tests.unit.test_formal_correctness import from_suite
from tests.unit.test_m1_d_verifier import PROFILE, _PortableReader, _Suite
from tests.unit.test_m1_worker_adapters import _base_payload, _FixtureCorrectnessProducer


class MemoryJournal:
    """Explicit CPU fixture; PostgreSQL behavior is tested separately."""

    def __init__(self, suite, payload, round_):
        self.job_id = UUID(payload["_job_context"]["job_id"])
        self.owner = dict(executor_id="fixture", token=suite.context.lease_id,
                          lease_id=suite.context.lease_id,
                          fencing_token=suite.context.fencing_token)
        self.job = {"payload": {
            "candidate_id": str(suite.context.candidate_id),
            "artifact_id": str(suite.context.artifact_id),
            "artifact_hash": suite.context.artifact_hash,
            "artifact_family_hash": round_.artifact_family_hash,
        }, "resource_id": suite.context.resource_id}
        repo = SimpleNamespace(connection=lambda: nullcontext(None))
        self.lease = SimpleNamespace(assert_live=Mock(), jobs=SimpleNamespace(
            claims=SimpleNamespace(dispatcher=SimpleNamespace(repository=repo))))
        self.events = {}
        self.result = None
        self.fail_settlement = False

    def _locked(self, conn, input_hash):
        return self.job, self.events

    def begin(self, input_hash):
        if self.events:
            return self.events, False
        self.events["formal_correctness_invoking"] = {"input_hash": input_hash}
        return self.events, True

    def record_result(self, input_hash, result, *, wall_seconds):
        assert wall_seconds >= 0
        self.result = result
        self.events["formal_correctness_result"] = result

    def mark_unknown(self, input_hash):
        self.events["formal_correctness_unknown"] = True

    def finalize_recorded_result(self, input_hash):
        if self.fail_settlement:
            raise RuntimeError("settlement unavailable")
        return self.result


def setup(tmp_path, delta=0.0):
    suite = _Suite(tmp_path / "evidence", candidate_delta=delta)
    materials = from_suite(suite)
    round_ = materials["round_authority"].model_copy(update={"adapter_profile": PROFILE})
    payload = _base_payload(suite)
    payload["round_id"] = str(round_.round_id)
    producer = _FixtureCorrectnessProducer(suite)
    producer.produce_manual_correctness_evidence = Mock(
        wraps=producer.produce_manual_correctness_evidence,
    )
    adapter = M1KernelCorrectnessWorkerAdapter(
        profile=PROFILE, protocol=suite.protocol, reader=_PortableReader(suite.root),
        producer=producer, evidence_root=suite.root,
    )
    journal = MemoryJournal(suite, payload, round_)
    consumer = FormalCorrectnessConsumer(journal, adapter, enabled=True)
    kwargs = dict(load_materials=lambda: (round_, materials["members"], payload),
                  output_dir=suite.root)
    return consumer, journal, producer, kwargs


@pytest.mark.parametrize("delta,verdict", [(0.0, "correct"), (0.25, "incorrect")])
def test_real_m1_adapter_verifies_outputs_once(tmp_path, delta, verdict):
    consumer, journal, producer, kwargs = setup(tmp_path, delta)
    result = consumer.execute_once(**kwargs)
    assert result.verdict == verdict
    assert consumer.execute_once(**kwargs) == result
    assert producer.produce_manual_correctness_evidence.call_count == 1
    assert "formal_correctness_unknown" not in journal.events


def test_settlement_failure_replays_result_without_producer(tmp_path):
    consumer, journal, producer, kwargs = setup(tmp_path)
    journal.fail_settlement = True
    with pytest.raises(RuntimeError, match="settlement"):
        consumer.execute_once(**kwargs)
    journal.fail_settlement = False
    assert consumer.execute_once(**kwargs).verdict == "correct"
    assert producer.produce_manual_correctness_evidence.call_count == 1


def test_unknown_producer_failure_cannot_retry(tmp_path):
    consumer, journal, producer, kwargs = setup(tmp_path)
    producer.produce_manual_correctness_evidence.side_effect = RuntimeError("producer lost")
    with pytest.raises(RuntimeError, match="producer lost"):
        consumer.execute_once(**kwargs)
    with pytest.raises(Conflict, match="uncertain"):
        consumer.execute_once(**kwargs)
    assert producer.produce_manual_correctness_evidence.call_count == 1


def test_wrong_claimed_artifact_never_invokes_producer(tmp_path):
    consumer, journal, producer, kwargs = setup(tmp_path)
    journal.job["payload"]["artifact_hash"] = "sha256:" + "f" * 64
    with pytest.raises(Conflict, match="claimed Job"):
        consumer.execute_once(**kwargs)
    assert not journal.events
    producer.produce_manual_correctness_evidence.assert_not_called()


def test_disabled_consumer_never_loads_materials(tmp_path):
    consumer, _, producer, kwargs = setup(tmp_path)
    consumer.enabled = False
    loader = Mock(side_effect=AssertionError("must not load"))
    with pytest.raises(Conflict, match="disabled"):
        consumer.execute_once(**{**kwargs, "load_materials": loader})
    loader.assert_not_called()
    producer.produce_manual_correctness_evidence.assert_not_called()


def test_changed_materials_after_journal_prevent_execution(tmp_path):
    consumer, journal, producer, kwargs = setup(tmp_path)
    original = kwargs["load_materials"]()
    changed = (*original[:2], {**original[2], "extra": "changed"})
    loader = Mock(side_effect=[original, changed])
    with pytest.raises(Conflict, match="changed before"):
        consumer.execute_once(**{**kwargs, "load_materials": loader})
    producer.produce_manual_correctness_evidence.assert_not_called()
    assert "formal_correctness_unknown" in journal.events
