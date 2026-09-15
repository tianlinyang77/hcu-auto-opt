# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hcuopt.adapters.profiles import AdapterProfileCatalog
from hcuopt.deployment import bw20_stage0_deployment as deployment
from hcuopt.deployment.bw20_stage0_guards import BW20SourceGuard
from hcuopt.deployment.bw20_stage0_harness import PROFILE
from hcuopt.deployment.bw20_stage0_staging import freeze_controller
from hcuopt.domain.errors import AdapterUnavailable, TargetNotReady
from hcuopt.evaluation.stage0_protocol import load_registered_stage0_protocol
from hcuopt.measurement.harness import MeasurementSafetyError
from hcuopt.targets import target_fingerprint
from tests.unit.test_bw20_stage0_adapter import Clock, Host, payload
from tests.unit.test_bw20_stage0_staging import source


@pytest.fixture
def setup(tmp_path):
    p = payload()
    # Explicit test fixture only: production Target Lock remains unchanged.
    p["target"]["blockers"] = []
    target = deployment.TargetSpec.model_validate(p["target"])
    p["target_fingerprint"] = target_fingerprint(target)
    admission = deployment.BW20MeasurementAdmission(
        target_hash=p["target_fingerprint"],
        workload_id=p["workload_id"],
        protocol_hash=load_registered_stage0_protocol("s0-g0-v2").sha256,
    )
    bundle = freeze_controller(source(tmp_path), tmp_path / "controller.tar")
    host = Host()
    adapter = deployment.compose_measurement_adapter(
        target=target,
        runner=host,
        bundle=bundle,
        manifest_sha256=bundle.manifest_sha256,
        admission=admission,
        clock_session_factory=lambda *args: Clock(),
    )
    return p, admission, bundle, host, adapter


def test_composition_is_local_and_does_not_register_partial_profile(setup):
    p, _, _, host, adapter = setup
    assert host.ticks == 100
    adapter.assert_admission(p)
    with pytest.raises(AdapterUnavailable):
        AdapterProfileCatalog().require(PROFILE)


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", "demo"),
        ("adapter_profile", "nmz36-stage0-v2"),
        ("workload_id", "foreign"),
        ("protocol_version", "s0-g0-v1"),
        ("probe_type", "profiler"),
        ("probe_type", "hotpatch"),
        ("target_fingerprint", "sha256:" + "f" * 64),
    ],
)
def test_admission_rejects_job_scope_changes(setup, field, value):
    p, _, _, _, adapter = setup
    p[field] = value
    with pytest.raises(MeasurementSafetyError):
        adapter.assert_admission(p)


def test_open_stage0_blockers_cannot_be_waived(setup):
    p = payload()
    for blocker in p["target"]["blockers"]:
        if blocker["id"] == "device_isolation_not_reserved":
            blocker["status"] = "open"
            break
    target = deployment.TargetSpec.model_validate(p["target"])
    p["target_fingerprint"] = target_fingerprint(target)
    admission = replace(setup[1], target_hash=p["target_fingerprint"])
    with pytest.raises(TargetNotReady, match="open blockers"):
        admission.validate(target, p)


def test_admission_pins_registered_protocol_and_full_target(setup):
    p, admission, _, _, adapter = setup
    with pytest.raises(MeasurementSafetyError, match="protocol changed"):
        replace(admission, protocol_hash="sha256:" + "f" * 64).validate(adapter.target, p)
    p["target"]["source_baseline"]["commit"] = "f" * 40
    with pytest.raises(MeasurementSafetyError, match="job target"):
        adapter.assert_admission(p)


@pytest.mark.parametrize(
    "field,value",
    [
        ("manifest_sha256", "sha256:" + "f" * 64),
        ("session_budget_seconds", True),
        ("session_budget_seconds", 481),
        ("session_budget_seconds", 0),
    ],
)
def test_composition_rejects_wrong_source_and_budget(setup, field, value):
    _, admission, bundle, host, adapter = setup
    kwargs = dict(
        target=adapter.target,
        runner=host,
        bundle=bundle,
        manifest_sha256=bundle.manifest_sha256,
        admission=admission,
        clock_session_factory=lambda *args: Clock(),
    )
    kwargs[field] = value
    with pytest.raises(ValueError):
        deployment.compose_measurement_adapter(**kwargs)


def test_staged_sessions_reuse_original_guards_without_opening_containers(
    setup, monkeypatch, tmp_path
):
    p, _, bundle, host, adapter = setup
    calls = []
    p["_job_context"]["assert_live_lease"] = lambda: calls.append("lease")

    def stage(**kwargs):
        calls.append("stage")
        guard = BW20SourceGuard(
            runner=host,
            source_root=kwargs["plan"].source_root,
            manifest_sha256=bundle.manifest_sha256,
        )
        guard.observations.append({"fixture": True})
        return guard

    monkeypatch.setattr(deployment, "stage_controller", stage)
    builder = adapter.session_builder(p["_job_context"], tmp_path)
    first, second = builder("timer", None), builder("noise", 0)
    assert calls == ["lease", "stage", "lease"] * 2
    assert first.plan.source_root != second.plan.source_root
    assert first.plan.container_name != second.plan.container_name
    assert not first.create_attempted and not second.create_attempted
    assert first.assert_lease is p["_job_context"]["assert_live_lease"]
    assert first.assert_staging.pin == bundle.manifest_sha256
    assert len(builder.staging_attempts) == 2
    assert all(r["staged"] and r["session_returned"] for r in builder.staging_attempts)
    with pytest.raises(MeasurementSafetyError, match="reused"):
        builder("noise", 0)


@pytest.mark.parametrize("when", ["before", "after", "upload"])
def test_failed_staging_or_lost_lease_never_returns_a_session(setup, monkeypatch, tmp_path, when):
    p, _, _, _, adapter = setup
    calls = []

    def lease():
        calls.append("lease")
        if when == "before" or (when == "after" and calls.count("lease") == 2):
            raise RuntimeError("lost lease")

    def stage(**kwargs):
        calls.append("stage")
        if when == "upload":
            raise OSError("upload unavailable")
        return SimpleNamespace(observations=[])

    monkeypatch.setattr(deployment, "stage_controller", stage)
    p["_job_context"]["assert_live_lease"] = lease
    builder = adapter.session_builder(p["_job_context"], tmp_path)
    with pytest.raises((RuntimeError, OSError)):
        builder("timer", None)
    if when == "before":
        assert calls == ["lease"] and builder.staging_attempts == []
    else:
        assert builder.staging_attempts[0]["session_returned"] is False
        assert "error_type" in builder.staging_attempts[0]


def test_invalid_scope_cannot_upload_and_unknown_roles_are_rejected(setup, tmp_path):
    p, _, _, _, adapter = setup
    p["_job_context"]["lease_scope"] = "shared"
    with pytest.raises(MeasurementSafetyError):
        adapter.session_builder(p["_job_context"], tmp_path)
    p["_job_context"]["lease_scope"] = "exclusive"
    builder = adapter.session_builder(p["_job_context"], tmp_path)
    for args in (("profiler", 0), ("noise", True), ("noise", 10), ("timer", -1)):
        with pytest.raises(MeasurementSafetyError, match="role"):
            builder(*args)
    assert not builder.staging_attempts


def test_composed_fingerprint_uses_original_adapter_without_source_upload(
    setup, monkeypatch, tmp_path
):
    p, _, _, _, adapter = setup
    monkeypatch.setattr(
        deployment, "stage_controller", lambda **kwargs: pytest.fail("unexpected upload")
    )
    result = adapter.run_probe(p, tmp_path)
    assert result.cleanup_evidence["health"]["healthy"]
    diagnostic = json.loads(next(tmp_path.rglob("diagnostics.json")).read_text())
    assert diagnostic["staging_attempts"] == []
    assert diagnostic["sessions"] == []
    assert diagnostic["primary_evidence"]["sha256"] == result.raw_evidence_hash


def test_composed_probe_retains_failed_upload_in_original_diagnostics(setup, monkeypatch, tmp_path):
    p, _, _, _, adapter = setup
    p["probe_type"] = "noise"

    def fail_upload(**kwargs):
        raise OSError("fixture source upload failed")

    monkeypatch.setattr(deployment, "stage_controller", fail_upload)
    with pytest.raises(OSError, match="fixture source upload failed"):
        adapter.run_probe(p, tmp_path)
    diagnostic = json.loads(next(tmp_path.rglob("diagnostics.json")).read_text())
    assert len(diagnostic["staging_attempts"]) == 1
    attempt = diagnostic["staging_attempts"][0]
    assert attempt["error_type"] == "OSError"
    assert attempt["staged"] is False and attempt["session_returned"] is False
    assert diagnostic["sessions"] == []
    assert "primary_evidence" not in diagnostic
