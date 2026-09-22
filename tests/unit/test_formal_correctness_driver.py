# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hcuopt.deployment.formal_correctness_driver import FormalCorrectnessDriver
from hcuopt.domain.errors import Conflict
from tests.unit.test_formal_correctness_consumer import setup


def fixture(tmp_path):
    consumer, _, _, _ = setup(tmp_path)
    target = SimpleNamespace(adapter_profile=consumer.adapter.provenance.profile)
    compiler = SimpleNamespace(
        profiles=SimpleNamespace(require=Mock(return_value=SimpleNamespace(authority_refs=target))),
        authorization=SimpleNamespace(profiles=SimpleNamespace(target_profile=object())),
    )
    runtime = SimpleNamespace(
        claims=object(), dispatcher=SimpleNamespace(enabled=True),
        management=SimpleNamespace(coordinator=SimpleNamespace(compiler=compiler)),
        correctness_consumer=Mock(return_value=SimpleNamespace(execute_current_once=Mock(
            return_value="explicit-test-result",
        ))),
    )
    driver = FormalCorrectnessDriver(runtime, intent_id=None, worker_id="owner", claim_token=None)
    return driver, runtime, consumer.adapter


def test_driver_delegates_exact_prepared_journal(tmp_path):
    driver, runtime, adapter = fixture(tmp_path)
    journal = SimpleNamespace(lease=driver.lease)
    driver.prepare = Mock(return_value=journal)
    assert driver.execute_candidate(candidate_id=None, executor_id="executor", wall_seconds=30,
                                    adapter=adapter, output_dir=tmp_path) == "explicit-test-result"
    driver.prepare.assert_called_once_with(
        candidate_id=None, executor_id="executor", wall_seconds=30,
    )
    runtime.correctness_consumer.assert_called_once_with(journal=journal, adapter=adapter)
    runtime.correctness_consumer.return_value.execute_current_once.assert_called_once_with(
        output_dir=tmp_path,
    )


@pytest.mark.parametrize("fault", ["type", "profile", "producer_profile", "producer_kind"])
def test_wrong_adapter_rejected_before_resource_acquisition(tmp_path, fault):
    driver, runtime, adapter = fixture(tmp_path)
    driver.prepare = Mock()
    if fault == "type":
        adapter = object()
    elif fault == "profile":
        profile = driver.runtime.management.coordinator.compiler.profiles.require.return_value
        profile.authority_refs = SimpleNamespace(adapter_profile="another-profile")
    else:
        change = {"profile": "another-profile"} if fault == "producer_profile" else {
            "implementation_kind": "fake",
        }
        adapter.producer.provenance = adapter.producer.provenance.model_copy(update=change)
    with pytest.raises((TypeError, Conflict)):
        driver.execute_candidate(candidate_id=None, executor_id="executor", wall_seconds=30,
                                 adapter=adapter, output_dir=tmp_path)
    driver.prepare.assert_not_called()
    runtime.correctness_consumer.assert_not_called()


def test_foreign_journal_cannot_enter_consumer(tmp_path):
    driver, runtime, adapter = fixture(tmp_path)
    with pytest.raises(Conflict, match="another driver"):
        driver.execute_prepared(journal=SimpleNamespace(lease=object()), adapter=adapter,
                                output_dir=tmp_path)
    runtime.correctness_consumer.assert_not_called()
