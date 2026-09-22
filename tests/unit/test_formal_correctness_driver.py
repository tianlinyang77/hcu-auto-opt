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


@pytest.mark.parametrize("disabled", [True, False])
def test_bw20_entry_rejects_before_loading_or_hardware(tmp_path, disabled):
    driver, runtime, _ = fixture(tmp_path)
    runtime.dispatcher.enabled = not disabled
    journal = SimpleNamespace(lease=driver.lease if disabled else object())
    with pytest.raises(Conflict, match="disabled or journal is foreign"):
        driver.execute_bw20_prepared(
            journal=journal, source_root=tmp_path,
            trusted_evidence_root=tmp_path, output_dir=tmp_path,
        )
    runtime.correctness_consumer.assert_not_called()


def test_bw20_entry_uses_durable_target_and_formal_producer(monkeypatch, tmp_path):
    from hcuopt.contracts.platform_v1 import TargetSpec
    from hcuopt.deployment import bw20_m1_correctness_worker as bw20
    from hcuopt.storage.formal_correctness_materials import PostgresFormalCorrectnessMaterialReader

    driver, runtime, adapter = fixture(tmp_path)
    driver.lease.assert_live = Mock()
    journal = SimpleNamespace(lease=driver.lease, job_id="job", owner={})
    target_payload = {"explicit": "database-fixture"}
    load = Mock(return_value=(None, None, {"target": target_payload}))
    monkeypatch.setattr(PostgresFormalCorrectnessMaterialReader, "load", load)
    target = object()
    validate = Mock(return_value=target)
    monkeypatch.setattr(TargetSpec, "model_validate", validate)
    registry = SimpleNamespace(require=Mock(return_value=adapter))
    factory = Mock(return_value=registry)
    monkeypatch.setattr(bw20, "build_bw20_m1_correctness_registry", factory)
    assert driver.execute_bw20_prepared(
        journal=journal, source_root=tmp_path, trusted_evidence_root=tmp_path,
        output_dir=tmp_path,
    ) == "explicit-test-result"
    driver.lease.assert_live.assert_called_once_with("job")
    validate.assert_called_once_with(target_payload)
    assert factory.call_args.kwargs["formal"] is True
    assert factory.call_args.kwargs["target"] is target
    registry.require.assert_called_once_with("kernel_correctness")
    runtime.correctness_consumer.assert_called_once_with(journal=journal, adapter=adapter)
