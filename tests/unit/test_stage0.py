import unittest

from dcuopt.domain.enums import (
    GateResult,
    HotPatchCapability,
    ProfilerCapability,
    ProjectMode,
)
from dcuopt.domain.errors import ContractError
from dcuopt.domain.models import Stage0Evidence
from dcuopt.stage0 import evaluate_stage0


def evidence(**overrides: object) -> Stage0Evidence:
    values: dict[str, object] = {
        "measurement": GateResult.PASS,
        "profiler": ProfilerCapability.FULL,
        "hot_patch": HotPatchCapability.HOT_PATCH,
        "hardware_fingerprint": "hw",
        "software_fingerprint": "sw",
        "timer_resolution_ns": 100.0,
        "noise_sigma_ns": 200.0,
        "noise_cv": 0.004,
        "mde_ratio": 0.012,
    }
    values.update(overrides)
    return Stage0Evidence(**values)  # type: ignore[arg-type]


class Stage0Tests(unittest.TestCase):
    def test_measurement_failure_stops_project(self) -> None:
        report = evaluate_stage0(
            evidence(
                measurement=GateResult.FAIL,
                timer_resolution_ns=None,
                noise_sigma_ns=None,
                noise_cv=None,
                mde_ratio=None,
            )
        )
        self.assertEqual(report.mode, ProjectMode.STOPPED_MEASUREMENT)
        self.assertFalse(report.automatic_release_allowed)

    def test_profiler_degradation_requires_manual_intake(self) -> None:
        report = evaluate_stage0(evidence(profiler=ProfilerCapability.DEGRADED))
        self.assertEqual(report.mode, ProjectMode.DEGRADED_MANUAL_INTAKE)

    def test_missing_profiler_leaves_config_only(self) -> None:
        report = evaluate_stage0(evidence(profiler=ProfilerCapability.NONE))
        self.assertEqual(report.mode, ProjectMode.CONFIG_ONLY)

    def test_full_probes_allow_full_mvp_but_not_automatic_release(self) -> None:
        report = evaluate_stage0(evidence())
        self.assertEqual(report.mode, ProjectMode.FULL_MVP)
        self.assertFalse(report.automatic_release_allowed)

    def test_measurement_pass_requires_calibration_numbers(self) -> None:
        with self.assertRaises(ContractError):
            evidence(timer_resolution_ns=None)


if __name__ == "__main__":
    unittest.main()

