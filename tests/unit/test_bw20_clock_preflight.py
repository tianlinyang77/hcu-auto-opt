# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from types import SimpleNamespace
from uuid import uuid4

import pytest

from hcuopt.deployment.bw20_clock_preflight import (
    assess_clock_state,
    collect_clock_preflight,
    parse_levels,
)


@pytest.fixture
def raw():
    return dict(host="github-bw20", pci="0000:b1:00.0", boot_id=str(uuid4()),
                scope="readonly_non_atomic_observation", files={
                    "numa_node": "4\n", "power_dpm_force_performance_level": "auto\n",
                    "pp_sclk_od": "0\n", "pp_mclk_od": "0\n", "pp_gfx_boost": "0\n",
                    "pp_dpm_sclk": "0: 600Mhz *\n1: 1500Mhz\n",
                    "pp_dpm_mclk": "0: 1800Mhz * (DPM disabled)\n"})


def test_compatible_observation_is_not_authority_or_complete_policy(raw):
    report = assess_clock_state(raw)
    assert report["observed_prerequisites_match"]
    assert report["proposed_sclk_index"] == 1  # Never hard-code level10 from another table.
    for field in ("execution_allowed", "restoration_verified", "enabled_mask_verified",
                  "stage0_accepted", "automatic_release_allowed"):
        assert report[field] is False


@pytest.mark.parametrize("field,value", [
    ("power_dpm_force_performance_level", "manual"), ("pp_sclk_od", "1"),
    ("pp_mclk_od", "1"), ("pp_gfx_boost", "1"), ("pp_gfx_boost", "unknown"),
    ("pp_dpm_sclk", "0: 600Mhz *"), ("pp_dpm_mclk", "0: 1800Mhz *"),
    ("pp_dpm_mclk", "0: 1700Mhz * (DPM disabled)"),
])
def test_incompatible_policy_never_proposed_as_matching(raw, field, value):
    raw["files"][field] = value
    report = assess_clock_state(raw)
    assert not report["observed_prerequisites_match"]
    assert report["rejection_reasons"]


@pytest.mark.parametrize("text", ["", "0: 600Mhz", "0: 600Mhz *\n0: 1500Mhz",
                                     "0: 600Mhz *\n1: 1500Mhz *", "1: 1500Mhz *",
                                     "0: 600Mhz * (DPM disabled)\n1: 1500Mhz",
                                     "0: 600Mhz *\n1: 600Mhz", "0: unknown *"])
def test_ambiguous_driver_output_rejected(text):
    with pytest.raises(ValueError):
        parse_levels(text)


def test_wrong_target_rejected(raw):
    raw["pci"] = "0000:b2:00.0"
    with pytest.raises(ValueError):
        assess_clock_state(raw)


def test_collector_checks_exit_status_without_hardware_writes():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1, stdout=b"{}", stderr=b"permission denied")

    runner = SimpleNamespace(host="10.17.1.20", user="github", port=22, run=run)
    with pytest.raises(RuntimeError):
        collect_clock_preflight(runner)
    assert calls[0][:2] == ("python3", "-c")
    assert "--set" not in calls[0][2]
    assert "open(str(path),encoding='utf-8')" in calls[0][2]
