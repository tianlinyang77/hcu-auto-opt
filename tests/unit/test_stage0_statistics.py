from __future__ import annotations

import math
from dataclasses import replace

import pytest

from hcuopt.evaluation.stage0_statistics import (
    NormalizedSample,
    SignalStatistics,
    Stage0StatisticsError,
    bonferroni_alpha,
    bootstrap_mean_ci,
    compute_abba_effect,
    compute_noise_statistics,
    derive_bootstrap_seed,
    known_signal_passes,
    mark_hampel_outliers,
    normalize_device_samples,
    null_signal_passes,
    recompute_clock_calibration,
    with_hampel_flags,
)

EVIDENCE_HASH = "sha256:" + "a" * 64


def test_clock_calibration_is_recomputed_from_raw_points() -> None:
    result = recompute_clock_calibration(
        [
            {
                "point_ordinal": 0,
                "device_ticks": 0,
                "host_started_monotonic_ns": 98,
                "host_finished_monotonic_ns": 102,
            },
            {
                "point_ordinal": 1,
                "device_ticks": 10,
                "host_started_monotonic_ns": 109,
                "host_finished_monotonic_ns": 113,
            },
            {
                "point_ordinal": 2,
                "device_ticks": 20,
                "host_started_monotonic_ns": 118,
                "host_finished_monotonic_ns": 122,
            },
        ],
        resolution_tick_deltas=[2, 3, 2],
    )

    assert result.ns_per_tick == pytest.approx(1.0)
    assert result.timer_resolution_ns == 2.0
    assert result.max_residual_ns == pytest.approx(2.0 + 2.0 / 3.0)
    assert result.point_count == 3


def test_clock_calibration_rejects_bad_raw_point_order_and_intervals() -> None:
    with pytest.raises(Stage0StatisticsError, match="positive host interval"):
        recompute_clock_calibration(
            [
                {
                    "point_ordinal": 0,
                    "device_ticks": 0,
                    "host_started_monotonic_ns": 100,
                    "host_finished_monotonic_ns": 100,
                },
                {
                    "point_ordinal": 1,
                    "device_ticks": 1,
                    "host_started_monotonic_ns": 101,
                    "host_finished_monotonic_ns": 102,
                },
                {
                    "point_ordinal": 2,
                    "device_ticks": 2,
                    "host_started_monotonic_ns": 102,
                    "host_finished_monotonic_ns": 103,
                },
            ],
            resolution_tick_deltas=[1, 1, 1],
        )


def test_clock_calibration_rejects_huge_integer_inputs_without_overflow() -> None:
    points = [
        {
            "point_ordinal": ordinal,
            "device_ticks": 10**1000 if ordinal == 2 else ordinal,
            "host_started_monotonic_ns": 100 + ordinal * 10,
            "host_finished_monotonic_ns": 102 + ordinal * 10,
        }
        for ordinal in range(3)
    ]

    with pytest.raises(Stage0StatisticsError, match="at most"):
        recompute_clock_calibration(points, resolution_tick_deltas=[1, 1, 1])

    with pytest.raises(Stage0StatisticsError, match="uint64"):
        recompute_clock_calibration(
            [
                {
                    "point_ordinal": ordinal,
                    "device_ticks": ordinal,
                    "host_started_monotonic_ns": 100 + ordinal * 10,
                    "host_finished_monotonic_ns": 102 + ordinal * 10,
                }
                for ordinal in range(3)
            ],
            resolution_tick_deltas=[1, 1, 10**1000],
        )


def _raw_sample(
    acquisition: int,
    *,
    restart: int = 0,
    started: int = 10,
    finished: int = 110,
    batch: int = 10,
    arm: str | None = None,
    segment: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "restart_ordinal": restart,
        "acquisition_ordinal": acquisition,
        "started_device_ticks": started,
        "finished_device_ticks": finished,
        "batch_iterations": batch,
    }
    if arm is not None:
        result["arm"] = arm
    if segment is not None:
        result["segment"] = segment
    return result


def _normalized(values: list[float]) -> tuple[NormalizedSample, ...]:
    return tuple(
        NormalizedSample(
            restart_ordinal=index,
            acquisition_ordinal=index,
            sample_ns=value,
        )
        for index, value in enumerate(values)
    )


def _abba_samples(
    effects: list[float],
    *,
    samples_per_segment: int = 1,
) -> tuple[NormalizedSample, ...]:
    samples: list[NormalizedSample] = []
    acquisition = 0
    for restart, effect in enumerate(effects):
        for segment in ("A1", "B1", "B2", "A2"):
            arm = segment[0]
            value = 100.0 if arm == "A" else 100.0 * (1 + effect)
            for _ in range(samples_per_segment):
                samples.append(
                    NormalizedSample(
                        restart_ordinal=restart,
                        acquisition_ordinal=acquisition,
                        sample_ns=value,
                        arm=arm,
                        segment=segment,
                    )
                )
                acquisition += 1
    return tuple(samples)


def test_device_ticks_are_normalized_per_batch_iteration() -> None:
    samples = normalize_device_samples(
        [
            _raw_sample(0, finished=210, batch=10),
            _raw_sample(1, restart=1, started=20, finished=120, batch=5),
        ],
        ns_per_tick=2.5,
    )

    assert [item.sample_ns for item in samples] == [50.0, 50.0]
    assert [item.restart_ordinal for item in samples] == [0, 1]


def test_sample_normalization_rejects_huge_integer_inputs_without_overflow() -> None:
    sample = _raw_sample(0)
    sample["finished_device_ticks"] = 10**1000

    with pytest.raises(Stage0StatisticsError, match="at most"):
        normalize_device_samples([sample], ns_per_tick=1.0)


@pytest.mark.parametrize("missing", ["started_device_ticks", "finished_device_ticks"])
def test_normalization_never_falls_back_when_device_ticks_are_missing(missing: str) -> None:
    sample = _raw_sample(0)
    sample.pop(missing)

    with pytest.raises(Stage0StatisticsError, match=f"missing {missing}"):
        normalize_device_samples([sample], ns_per_tick=1.0)


@pytest.mark.parametrize(
    ("samples", "message"),
    [
        ([_raw_sample(0), _raw_sample(0, restart=1)], "duplicate acquisition"),
        ([_raw_sample(1)], "contiguous"),
        ([_raw_sample(0, restart=1)], "restart_ordinal must be contiguous"),
        (
            [_raw_sample(0, restart=0), _raw_sample(1, restart=1), _raw_sample(2, restart=0)],
            "must not be interleaved",
        ),
        ([_raw_sample(0, started=10, finished=10)], "must exceed"),
    ],
)
def test_normalization_rejects_duplicate_disordered_or_zero_samples(
    samples: list[dict[str, object]], message: str
) -> None:
    with pytest.raises(Stage0StatisticsError, match=message):
        normalize_device_samples(samples, ns_per_tick=1.0)


@pytest.mark.parametrize("scale", [0, -1, math.inf, math.nan])
def test_normalization_rejects_invalid_tick_scale(scale: float) -> None:
    with pytest.raises(Stage0StatisticsError):
        normalize_device_samples([_raw_sample(0)], ns_per_tick=scale)


def test_noise_golden_statistics_use_restart_means_and_ddof_one() -> None:
    result = compute_noise_statistics(
        _normalized([100.0, 110.0, 90.0, 100.0]),
        evidence_hash=EVIDENCE_HASH,
    )

    expected_sigma = math.sqrt(200 / 3)
    expected_mde = (
        (1.9599639845400536 + 0.8416212335729144) * expected_sigma * math.sqrt(2 / 4) / 100.0
    )
    assert result.restart_means_ns == (100.0, 110.0, 90.0, 100.0)
    assert result.mean_ns == 100.0
    assert result.sigma_ns == pytest.approx(expected_sigma)
    assert result.cv == pytest.approx(expected_sigma / 100.0)
    assert result.mde_fraction == pytest.approx(expected_mde)
    assert (result.ci_lower_ns, result.ci_upper_ns) == (92.5, 107.5)


def test_noise_statistics_require_two_restarts_and_finite_positive_samples() -> None:
    with pytest.raises(Stage0StatisticsError, match="two independent restarts"):
        compute_noise_statistics(_normalized([100.0]), evidence_hash=EVIDENCE_HASH)

    for value in (0.0, math.inf, math.nan):
        with pytest.raises(Stage0StatisticsError, match="finite and positive"):
            compute_noise_statistics(_normalized([100.0, value]), evidence_hash=EVIDENCE_HASH)


def test_percentile_bootstrap_is_deterministic_and_hash_bound() -> None:
    first = bootstrap_mean_ci(
        [1.0, 2.0, 3.0, 4.0],
        evidence_hash=EVIDENCE_HASH,
        alpha=0.05,
    )
    second = bootstrap_mean_ci(
        [1.0, 2.0, 3.0, 4.0],
        evidence_hash=EVIDENCE_HASH,
        alpha=0.05,
    )

    assert first == second == (1.5, 3.5)
    assert derive_bootstrap_seed(EVIDENCE_HASH, domain="mean") == derive_bootstrap_seed(
        EVIDENCE_HASH, domain="mean"
    )
    with pytest.raises(Stage0StatisticsError, match="lowercase sha256"):
        derive_bootstrap_seed("not-a-hash", domain="mean")


def test_hampel_marks_but_does_not_remove_or_change_samples() -> None:
    samples = _normalized([100.0, 100.0, 100.0, 1000.0])
    flags = mark_hampel_outliers([item.sample_ns for item in samples])
    flagged = with_hampel_flags(samples)
    result = compute_noise_statistics(samples, evidence_hash=EVIDENCE_HASH)

    assert flags == (False, False, False, True)
    assert len(flagged) == len(samples)
    assert [item.sample_ns for item in flagged] == [item.sample_ns for item in samples]
    assert [item.outlier for item in flagged] == list(flags)
    assert result.mean_ns == 325.0
    assert result.outlier_count == 1
    assert result.outlier_fraction == 0.25


def test_abba_known_signal_uses_paired_restart_effects_and_bonferroni_alpha() -> None:
    result = compute_abba_effect(
        _abba_samples([0.20, 0.21, 0.19, 0.20]),
        evidence_hash=EVIDENCE_HASH,
        alpha=bonferroni_alpha(),
    )

    assert result.effect_fraction == pytest.approx(0.20)
    assert result.restart_effects == pytest.approx((0.20, 0.21, 0.19, 0.20))
    assert result.baseline_restart_means_ns == (100.0,) * 4
    assert result.comparison_restart_means_ns == pytest.approx((120.0, 121.0, 119.0, 120.0))
    assert result.ci_lower == pytest.approx(0.1925)
    assert result.ci_upper == pytest.approx(0.2075)
    assert known_signal_passes(result) is True
    assert bonferroni_alpha() == 0.025


def test_abba_null_signal_requires_the_entire_interval_inside_equivalence_margin() -> None:
    result = compute_abba_effect(
        _abba_samples([-0.01, 0.0, 0.01, 0.0]),
        evidence_hash=EVIDENCE_HASH,
    )

    assert null_signal_passes(result) is True
    assert known_signal_passes(result) is False
    outside = SignalStatistics(
        effect_fraction=0.0,
        ci_lower=-0.02,
        ci_upper=0.031,
        restart_effects=(0.0, 0.0),
        baseline_restart_means_ns=(100.0, 100.0),
        comparison_restart_means_ns=(100.0, 100.0),
        outlier_count=0,
        sample_count=8,
    )
    assert null_signal_passes(outside) is False


def test_abba_rejects_wrong_order_unequal_segments_and_missing_metadata() -> None:
    ordered = list(_abba_samples([0.1, 0.1], samples_per_segment=1))
    ordered[1], ordered[2] = ordered[2], ordered[1]
    ordered = [replace(item, acquisition_ordinal=index) for index, item in enumerate(ordered)]
    with pytest.raises(Stage0StatisticsError, match="A1-B1-B2-A2"):
        compute_abba_effect(ordered, evidence_hash=EVIDENCE_HASH)

    unequal = list(_abba_samples([0.1, 0.1], samples_per_segment=1))
    unequal.insert(
        1,
        replace(unequal[0], acquisition_ordinal=1),
    )
    unequal = [replace(item, acquisition_ordinal=index) for index, item in enumerate(unequal)]
    with pytest.raises(Stage0StatisticsError, match="equal sample counts"):
        compute_abba_effect(unequal, evidence_hash=EVIDENCE_HASH)

    missing = list(_abba_samples([0.1, 0.1]))
    missing[0] = replace(missing[0], arm=None)
    with pytest.raises(Stage0StatisticsError, match="missing ABBA"):
        compute_abba_effect(missing, evidence_hash=EVIDENCE_HASH)


def test_normalization_validates_abba_arm_segment_pair() -> None:
    sample = _raw_sample(0, arm="comparison", segment="A1")
    with pytest.raises(Stage0StatisticsError, match="does not match"):
        normalize_device_samples([sample], ns_per_tick=1.0)


def test_known_signal_confidence_floor_is_strict() -> None:
    result = SignalStatistics(
        effect_fraction=0.10,
        ci_lower=0.03,
        ci_upper=0.15,
        restart_effects=(0.10, 0.10),
        baseline_restart_means_ns=(100.0, 100.0),
        comparison_restart_means_ns=(110.0, 110.0),
        outlier_count=0,
        sample_count=8,
    )

    assert known_signal_passes(result) is False
