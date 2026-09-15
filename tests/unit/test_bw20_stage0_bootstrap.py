# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json

import pytest
from fastapi.testclient import TestClient

from hcuopt.deployment import bw20_stage0_bootstrap as bootstrap
from hcuopt.deployment.bw20_auto_clock_session import BW20AutoClockSessionFactory
from hcuopt.deployment.bw20_auto_observation_session import (
    BW20AutoObservationSessionFactory,
)
from hcuopt.deployment.bw20_clock_backend_review import BW20ClockBackendReviewManifest
from hcuopt.deployment.bw20_clock_journal import ClockJournal
from hcuopt.deployment.bw20_runtime_verify import checked_digest
from hcuopt.domain.errors import AdapterUnavailable, TargetConfigError, TargetNotReady
from hcuopt.measurement.evidence import canonical_json_bytes
from tests.unit.test_bw20_auto_clock_session import SimulatedBackend
from tests.unit.test_bw20_runtime_binding import fixture_record
from tests.unit.test_bw20_stage0_adapter import Host
from tests.unit.test_bw20_stage0_capabilities import case  # noqa: F401


@pytest.fixture
def deployment(case, tmp_path, monkeypatch):  # noqa: F811
    _, target, cfg, admission, _, _ = case
    root = tmp_path / "prepared"
    root.mkdir()
    controller = tmp_path / "controller"
    controller.mkdir()
    archive = tmp_path / "controller.tar"
    archive.write_bytes(b"cpu-fixture-only")
    target_file = root / "target.json"
    target_file.write_bytes(canonical_json_bytes(target))
    (root / "runtime-profile.json").write_bytes(canonical_json_bytes(cfg))
    # Reuse the original schema; no model workload is executed in this fixture.
    from hcuopt.evaluation.sglang_smoke import SGLangWorkloadSpec
    workload = SGLangWorkloadSpec(workload_id=admission.workload_id, target_id=target.target_id,
                                 model_path="/model", prompt="test", cookbook_commit="a" * 40,
                                 cookbook_paths=["test.md"])
    (root / "workload.json").write_bytes(canonical_json_bytes(workload))
    (root / "preparation.json").write_text(json.dumps({
        "input_hashes": {"target.json": checked_digest(target_file)},
        "controller_manifest_sha256": "sha256:" + "a" * 64,
    }))
    runtime_file = tmp_path / "runtime.json"
    runtime_file.write_text(json.dumps(fixture_record()))
    # Only disk/source verification is a test double; seven-route composition,
    # Target/Profile admission, API catalog and Worker wiring are original code.
    inputs = dict(target_fingerprint=admission.target_hash,
                  profile_sha256=checked_digest(root / "runtime-profile.json"),
                  controller_checks=[{"file_count": 1}])
    monkeypatch.setattr(bootstrap, "BW20PreparedInputGuard", lambda *a: lambda: inputs)
    config = bootstrap.DeploymentConfig(
        prepared_root=root, controller_root=controller, controller_archive=archive,
        preparation_sha256=checked_digest(root / "preparation.json"),
        archive_sha256=checked_digest(archive), runtime_binding=runtime_file,
        runtime_binding_sha256=checked_digest(runtime_file),
        protocol_sha256=admission.protocol_hash, evidence_root=tmp_path,
    )
    path = tmp_path / "deployment.json"
    path.write_text(config.model_dump_json())
    return bootstrap.load_deployment(
        path,
        config_sha256=checked_digest(path),
        runner=Host(),
        clock_session_factory=BW20AutoObservationSessionFactory(),
    )


def test_check_seven_real_routes_has_no_acceptance_or_service_side_effect(deployment):
    result = deployment.check()
    assert result["target_admitted"] is True
    assert result["clock_control_bound"] is False
    assert result["measurement_clock_policy_bound"] is True
    assert result["measurement_clock_policy"] == "host_auto_observe_only_v1"
    assert len(result["routes"]) == 7
    assert result["services_started"] is result["hcu_used"] is result["stage0_accepted"] is False
    assert deployment.registry.profile == "bw20-stage0-v1"
    for adapter in deployment.registry.stage0_probe._routes.values():
        assert adapter.provenance.implementation_kind == "real"


@pytest.mark.parametrize("field", ["config_path", "runtime_binding", "controller_archive"])
def test_changed_pin_blocks_start(deployment, field):
    path = deployment.config_path if field == "config_path" else getattr(deployment.config, field)
    path.write_bytes(b"changed")
    with pytest.raises(ValueError):
        deployment.make_worker(api_url="http://127.0.0.1:8820", worker_id="fixture")


def test_target_change_and_other_target_refused(deployment):
    with pytest.raises(TargetConfigError):
        deployment.targets.load("nmz36")
    deployment.targets.path.write_bytes(b"{}")
    with pytest.raises(ValueError):
        deployment.check()


def test_unresolved_target_never_constructs_worker_or_app(deployment, monkeypatch):
    monkeypatch.setattr(deployment, "check",
                        lambda: {"target_admitted": False, "rejection": "open"})
    with pytest.raises(TargetNotReady, match="open"):
        deployment.make_worker(api_url="http://127.0.0.1:8820", worker_id="fixture")
    with pytest.raises(TargetNotReady, match="open"):
        deployment.make_application(repository=object())


@pytest.mark.parametrize("url", ["https://127.0.0.1:8820", "http://other:8820",
                                "http://user:pass@127.0.0.1:8820", "http://127.0.0.1",
                                "http://127.0.0.1:8820/x", "http://127.0.0.1:8820?x=1"])
def test_worker_endpoint_scope(deployment, url):
    with pytest.raises(ValueError):
        deployment.make_worker(api_url=url, worker_id="fixture")


def test_worker_is_unregistered_until_explicit_run(deployment):
    worker = deployment.make_worker(
        api_url="http://127.0.0.1:8820", worker_id="fixture"
    )
    try:
        assert worker.registered is False
        assert worker.capabilities["resource_id"] == "bw20-sglang-0.5.12:hcu:7"
        assert worker.capabilities["adapter_profile"] == "bw20-stage0-v1"
        worker.resource_guard(worker.capabilities["resource_id"])
        with pytest.raises(ValueError):
            worker.resource_guard("other")
        assert deployment.clock_journal is None
    finally:
        worker.client.client.close()


def test_auto_worker_rejects_an_unrelated_clock_journal(deployment, tmp_path):
    replacement = ClockJournal(tmp_path / "replacement-clock.sqlite")
    with pytest.raises(ValueError, match="does not use a clock journal"):
        deployment.make_worker(
            api_url="http://127.0.0.1:8820",
            worker_id="fixture",
            clock_journal=replacement,
        )


def test_unbound_clock_backend_allows_check_only(deployment, tmp_path):
    unbound = bootstrap.load_deployment(
        deployment.config_path,
        config_sha256=deployment.config_sha256,
        runner=Host(),
    )
    report = unbound.check()
    assert report["inputs_verified"] is True
    assert report["clock_control_bound"] is False
    assert report["measurement_clock_policy_bound"] is False
    with pytest.raises(TargetNotReady, match="measurement clock policy"):
        unbound.make_application(repository=object())
    journal = ClockJournal(tmp_path / "unbound-clock.sqlite")
    with pytest.raises(TargetNotReady, match="measurement clock policy"):
        unbound.make_worker(
            api_url="http://127.0.0.1:8820",
            worker_id="fixture",
            clock_journal=journal,
        )


def test_arbitrary_callable_cannot_claim_clock_control_is_bound(deployment):
    with pytest.raises(ValueError, match="measurement clock policy"):
        bootstrap.load_deployment(
            deployment.config_path,
            config_sha256=deployment.config_sha256,
            runner=Host(),
            clock_session_factory=lambda *args: None,
        )


def test_api_uses_same_catalog_and_cannot_implicitly_migrate(deployment, monkeypatch):
    class Repository:
        def migrate(self):
            pytest.fail("implicit database migration")

    monkeypatch.setenv("HCUOPT_AUTO_MIGRATE", "true")
    app = deployment.make_application(repository=Repository())
    with TestClient(app) as client:
        response = client.get("/v1/adapter-profiles")
    assert response.status_code == 200
    assert [p["name"] for p in response.json()] == ["bw20-stage0-v1"]
    with pytest.raises(AdapterUnavailable):
        bootstrap.AdapterProfileCatalog((deployment.profile,)).require("fake-v1-control-flow-only")


def test_config_rejects_secrets_and_relative_paths(tmp_path):
    with pytest.raises(ValueError):
        bootstrap.DeploymentConfig.model_validate({"prepared_root": "relative", "password": "x"})


def test_optional_static_backend_review_is_display_only(deployment, tmp_path):
    backend = tmp_path / "reviewed-backend.bin"
    backend.write_bytes(b"static-review-only")
    review_journal = ClockJournal(tmp_path / "review-clock.sqlite")
    manifest = BW20ClockBackendReviewManifest(
        target_id=deployment.targets.target_id,
        target_fingerprint=deployment.check()["target_fingerprint"],
        backend_artifact=backend,
        backend_sha256=checked_digest(backend),
        journal_path=review_journal.path,
        authority_provider_id="operator/reviewed-clock-authority-v1",
    )
    manifest_path = tmp_path / "clock-backend-review.json"
    manifest_path.write_text(manifest.model_dump_json())
    config = deployment.config.model_copy(update={
        "clock_backend_review": manifest_path,
        "clock_backend_review_sha256": checked_digest(manifest_path),
    })
    config_path = tmp_path / "deployment-with-review.json"
    config_path.write_text(config.model_dump_json())

    reviewed = bootstrap.load_deployment(
        config_path,
        config_sha256=checked_digest(config_path),
        runner=Host(),
    )
    report = reviewed.check()
    assert report["clock_backend_static_review"]["static_inputs_verified"] is True
    assert report["clock_backend_static_review"]["clock_control_bound"] is False
    assert report["clock_backend_static_review"]["execution_allowed"] is False
    assert report["clock_control_bound"] is False
    assert report["measurement_clock_policy_bound"] is False
    review_journal.begin(
        resource_id="bw20-sglang-0.5.12:hcu:7",
        authorization_id="fixture-only",
        original={"mode": "auto"},
    )
    refreshed = reviewed.check()
    assert refreshed["clock_backend_static_review"]["journal_clear"] is False
    assert "clock_journal_requires_reconciliation" in (
        refreshed["clock_backend_static_review"]["review_blockers"]
    )
    with pytest.raises(TargetNotReady, match="measurement clock policy"):
        reviewed.make_application(repository=object())


def test_manual_clock_factory_is_refused_by_registered_auto_protocol(deployment, tmp_path):
    journal = ClockJournal(tmp_path / "manual-clock.sqlite")
    factory = BW20AutoClockSessionFactory(
        backend=SimulatedBackend(), journal=journal,
        authorization_id="fixture-only",
        assert_clock_authority=lambda context: None,
        assert_default_baseline=lambda baseline: None,
    )
    with pytest.raises(ValueError, match="conflicts with the registered auto protocol"):
        bootstrap.load_deployment(
            deployment.config_path,
            config_sha256=deployment.config_sha256,
            runner=Host(),
            clock_session_factory=factory,
        )


def test_auto_observation_policy_needs_no_clock_backend_or_journal(deployment):
    report = deployment.check()
    assert report["measurement_clock_policy_bound"] is True
    assert report["measurement_clock_policy"] == "host_auto_observe_only_v1"
    assert report["clock_control_bound"] is False
    assert deployment.clock_journal is None
    worker = deployment.make_worker(
        api_url="http://127.0.0.1:8820", worker_id="fixture"
    )
    try:
        worker.resource_guard(worker.capabilities["resource_id"])
    finally:
        worker.client.client.close()
    with pytest.raises(ValueError, match="does not use a clock journal"):
        deployment.make_worker(
            api_url="http://127.0.0.1:8820",
            worker_id="wrong-journal",
            clock_journal=ClockJournal(
                deployment.config.evidence_root / "unexpected-clock.sqlite"
            ),
        )
