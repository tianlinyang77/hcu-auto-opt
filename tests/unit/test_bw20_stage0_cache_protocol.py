# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json

import pytest

from hcuopt.domain.enums import Stage0ProbeType
from hcuopt.evaluation.stage0_protocol import load_registered_stage0_protocol
from hcuopt.evaluation.stage0_verifier import Stage0EvidenceError, Stage0Verifier
from hcuopt.measurement.models import MeasurementEvidenceV2
from tests.unit.test_bw20_stage0_runtime import TARGET
from tests.unit.test_stage0_verifier import _build_suite, _verifier_reader

VERSION = "s0-g0-bw20-v1"
VERSION_V2 = "s0-g0-bw20-v2"


def test_legacy_hashes_are_unchanged_and_bw20_adds_auto_observation_policy():
    v1 = load_registered_stage0_protocol("s0-g0-v1")
    v2 = load_registered_stage0_protocol("s0-g0-v2")
    assert v1.sha256 == "sha256:6638210463ef4cd35c1f15b9f8d6aa8047a59e605b6e2d3cf08e8cce500c34e4"
    assert v2.sha256 == "sha256:a976fdd6299e83d9571cf5f3aad93fe9d282fa1772563967a351fb245cb09d64"
    assert load_registered_stage0_protocol(VERSION).sha256 == (
        "sha256:9cd83c975208ad164789bc084aab42aa82a4683d988afdbe910cbc492d95fb6a"
    )
    assert load_registered_stage0_protocol(VERSION_V2).sha256 == (
        "sha256:7d7b1f533d75edb21647e87b3d96606112f76c80c9727243d85fd7f32e8cff54"
    )
    old = v2.protocol.model_dump()
    new = load_registered_stage0_protocol(VERSION).protocol.model_dump()
    assert old.pop("cache_policy") is None
    assert new.pop("cache_policy") == "allocator_reset_per_batch_v1"
    old.pop("protocol_version")
    new.pop("protocol_version")
    old_environment = old.pop("environment_gates")
    new_environment = new.pop("environment_gates")
    assert old == new
    assert old_environment["required_performance_level"] == "manual"
    assert old_environment["clock_validation_mode"] == "target_fixed"
    assert new_environment["required_performance_level"] == "auto"
    assert new_environment["clock_validation_mode"] == "observed_stable"


def test_new_policy_cannot_bypass_bw20_sidecars_or_run_on_nmz36(tmp_path):
    suite = _build_suite(tmp_path / "other", protocol_version=VERSION)
    with pytest.raises(Stage0EvidenceError, match="allocator receipts require BW20"):
        suite.verify()
    suite = _build_suite(tmp_path / "bw20", target=TARGET, protocol_version=VERSION)
    with pytest.raises(Stage0EvidenceError, match="missing BW20 diagnostic"):
        suite.verify()


def test_only_new_policy_uses_receipts_instead_of_claiming_cold_cache(tmp_path):
    suite = _build_suite(tmp_path, target=TARGET, protocol_version=VERSION)
    raw = suite.raw[Stage0ProbeType.NOISE]
    for observation in raw["observations"]:
        observation["telemetry"]["cache"].update(state="unknown", cleared_before_sample=False)
    evidence = MeasurementEvidenceV2.model_validate_json(json.dumps(raw))
    new = Stage0Verifier(suite.protocol, _verifier_reader(suite.root))
    legacy = Stage0Verifier(load_registered_stage0_protocol("s0-g0-v2"), new.reader)
    assert not any(
        code.startswith("cache_") for code, _ in new._environment_failures(TARGET, evidence)
    )
    assert {"cache_state_unknown", "cache_not_cleared"} <= {
        code for code, _ in legacy._environment_failures(TARGET, evidence)
    }
    for observation in raw["observations"]:
        observation["telemetry"]["cache"].update(state="flushed", cleared_before_sample=True)
    evidence = MeasurementEvidenceV2.model_validate_json(json.dumps(raw))
    assert "cache_scope_overclaim" in {
        code for code, _ in new._environment_failures(TARGET, evidence)
    }


def test_bw20_auto_policy_accepts_stable_observations_and_rejects_drift(tmp_path):
    suite = _build_suite(tmp_path, target=TARGET, protocol_version=VERSION)
    raw = suite.raw[Stage0ProbeType.NOISE]
    for observation in raw["observations"]:
        observation["telemetry"]["device"].update(
            performance_level="auto", sclk_mhz=600.0, mclk_mhz=1800.0
        )
        observation["telemetry"]["cache"].update(
            state="unknown", cleared_before_sample=False
        )
    verifier = Stage0Verifier(suite.protocol, _verifier_reader(suite.root))
    stable = MeasurementEvidenceV2.model_validate_json(json.dumps(raw))
    stable_codes = {
        code for code, _ in verifier._environment_failures(TARGET, stable)
    }
    assert not ({"sclk_drift_exceeded", "mclk_drift_exceeded",
                 "performance_level_mismatch"} & stable_codes)

    raw["observations"][-1]["telemetry"]["device"]["sclk_mhz"] = 900.0
    drifted = MeasurementEvidenceV2.model_validate_json(json.dumps(raw))
    assert "sclk_drift_exceeded" in {
        code for code, _ in verifier._environment_failures(TARGET, drifted)
    }


def test_bw20_v2_preserves_thresholds_but_treats_dvfs_clocks_as_diagnostics(tmp_path):
    old = load_registered_stage0_protocol(VERSION)
    new = load_registered_stage0_protocol(VERSION_V2)
    assert new.protocol.max_cv_ratio == old.protocol.max_cv_ratio == 0.02
    assert new.protocol.max_mde_ratio == old.protocol.max_mde_ratio == 0.03
    assert new.protocol.environment_gates.required_performance_level == "auto"
    assert new.protocol.environment_gates.clock_validation_mode == "performance_level_only"
    assert new.protocol.calibration_capture_mode == "measurement_process_atomic_v1"

    suite = _build_suite(tmp_path, target=TARGET, protocol_version=VERSION_V2)
    raw = suite.raw[Stage0ProbeType.NOISE]
    for ordinal, observation in enumerate(raw["observations"]):
        observation["telemetry"]["device"].update(
            performance_level="auto",
            sclk_mhz=600.0 + ordinal * 100.0,
            mclk_mhz=1800.0,
        )
        observation["telemetry"]["cache"].update(
            state="unknown", cleared_before_sample=False
        )
    evidence = MeasurementEvidenceV2.model_validate_json(json.dumps(raw))
    verifier = Stage0Verifier(suite.protocol, _verifier_reader(suite.root))
    codes = {code for code, _ in verifier._environment_failures(TARGET, evidence)}
    assert not ({"sclk_drift_exceeded", "mclk_drift_exceeded"} & codes)
    assert "performance_level_mismatch" not in codes

    raw["observations"][-1]["telemetry"]["device"]["performance_level"] = "manual"
    evidence = MeasurementEvidenceV2.model_validate_json(json.dumps(raw))
    assert "performance_level_mismatch" in {
        code for code, _ in verifier._environment_failures(TARGET, evidence)
    }
