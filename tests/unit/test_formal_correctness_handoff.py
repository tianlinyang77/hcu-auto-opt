# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import pytest

from hcuopt.deployment.formal_correctness_handoff import reverify_result
from hcuopt.domain.errors import Conflict
from tests.unit.test_formal_correctness_consumer import setup


@pytest.mark.parametrize("delta,expected", [(0.0, "correct"), (0.25, "incorrect")])
def test_handoff_rechecks_raw_outputs_without_reinvoking(tmp_path, delta, expected):
    consumer, _, producer, kwargs = setup(tmp_path, delta)
    result = consumer.execute_once(**kwargs)
    round_, members, payload = kwargs["load_materials"]()
    verified = reverify_result(consumer.adapter, round_, members, payload, result)
    assert verified.verdict == expected
    assert producer.produce_manual_correctness_evidence.call_count == 1


@pytest.mark.parametrize("fault", ["verdict", "hash", "synthetic"])
def test_handoff_rejects_changed_result(tmp_path, fault):
    consumer, _, _, kwargs = setup(tmp_path)
    result = consumer.execute_once(**kwargs)
    update = {"verdict": "incorrect"} if fault == "verdict" else (
        {"raw_evidence_hash": "sha256:" + "f" * 64} if fault == "hash" else {"synthetic": True}
    )
    round_, members, payload = kwargs["load_materials"]()
    with pytest.raises(Conflict):
        reverify_result(
            consumer.adapter, round_, members, payload, result.model_copy(update=update),
        )
