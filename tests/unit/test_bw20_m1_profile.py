# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from pathlib import Path
from types import SimpleNamespace

import pytest

from hcuopt.adapters.profiles import BW20_MANUAL_CANDIDATE_PROFILE
from hcuopt.deployment.bw20_m1_profile import compose_bw20_m1_measurement
from hcuopt.deployment.bw20_stage0_staging import ControllerBundle
from hcuopt.targets import load_target

ROOT = Path(__file__).resolve().parents[2]
HASH = "sha256:" + "a" * 64


def _target():
    return load_target(ROOT / "config/targets/bw20-sglang-0.5.12.yaml")


def _runner():
    return SimpleNamespace(host="10.17.1.20", user="github", port=22)


def _bundle(tmp_path):
    archive = tmp_path / "controller.tar"
    archive.write_bytes(b"fixture")
    return ControllerBundle(archive, HASH, HASH, 1)


def test_measurement_profile_composes_without_host_contact_or_catalog_registration(
    tmp_path,
) -> None:
    composition = compose_bw20_m1_measurement(
        target=_target(),
        runner=_runner(),
        controller_bundle=_bundle(tmp_path),
        controller_manifest_sha256=HASH,
        trusted_evidence_root=tmp_path,
        baseline_module_hash=HASH,
        initial_clock_state={"mode": "auto", "sclk_mhz": 600, "mclk_mhz": 1800},
    )

    assert composition.registry.profile == BW20_MANUAL_CANDIDATE_PROFILE
    assert composition.registry.require("measurement_harness").provenance.adapter_name == (
        "M1TrustedMeasurementHarness"
    )
    assert composition.registry.require("resource_cleaner") is composition.cleaner
    assert composition.workload_factory.sessions == []


def test_measurement_profile_rejects_unpinned_controller_bundle(tmp_path) -> None:
    with pytest.raises(ValueError, match="deployment pin"):
        compose_bw20_m1_measurement(
            target=_target(),
            runner=_runner(),
            controller_bundle=_bundle(tmp_path),
            controller_manifest_sha256="sha256:" + "b" * 64,
            trusted_evidence_root=tmp_path,
            baseline_module_hash=HASH,
            initial_clock_state={"mode": "auto", "sclk_mhz": 600, "mclk_mhz": 1800},
        )
