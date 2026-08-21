from __future__ import annotations

import hashlib
import math
import random
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from statistics import NormalDist, fmean, median, stdev
from typing import Any

_SHA256_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
_ABBA_SEGMENTS = ("A1", "B1", "B2", "A2")
_SEGMENT_ARM = {"A1": "A", "A2": "A", "B1": "B", "B2": "B"}
_INT64_MAX = (1 << 63) - 1
_UINT64_MAX = (1 << 64) - 1
_MAX_ORDINAL = 1_000_000


class Stage0StatisticsError(ValueError):
    """Raised when raw Stage 0 samples are not safe to analyze."""


@dataclass(frozen=True, slots=True)
class NormalizedSample:
    restart_ordinal: int
    acquisition_ordinal: int
    sample_ns: float
    arm: str | None = None
    segment: str | None = None
    outlier: bool = False


@dataclass(frozen=True, slots=True)
class NoiseStatistics:
    mean_ns: float
    sigma_ns: float
    cv: float
    mde_fraction: float
    ci_lower_ns: float
    ci_upper_ns: float
    restart_means_ns: tuple[float, ...]
    outlier_count: int
    sample_count: int

    @property
    def outlier_fraction(self) -> float:
        return self.outlier_count / self.sample_count


@dataclass(frozen=True, slots=True)
class SignalStatistics:
    effect_fraction: float
    ci_lower: float
    ci_upper: float
    restart_effects: tuple[float, ...]
    baseline_restart_means_ns: tuple[float, ...]
    comparison_restart_means_ns: tuple[float, ...]
    outlier_count: int
    sample_count: int

    @property
    def outlier_fraction(self) -> float:
        return self.outlier_count / self.sample_count


@dataclass(frozen=True, slots=True)
class ClockCalibrationStatistics:
    timer_resolution_ns: float
    ns_per_tick: float
    max_residual_ns: float
    point_count: int


def recompute_clock_calibration(
    points: Sequence[Mapping[str, Any]],
    *,
    resolution_tick_deltas: Sequence[int],
) -> ClockCalibrationStatistics:
    """Fit host midpoint against device ticks and derive timer inputs from raw data."""

    if len(points) < 3:
        raise Stage0StatisticsError("at least three clock calibration points are required")
    if len(resolution_tick_deltas) < 3:
        raise Stage0StatisticsError("at least three timer-resolution observations are required")
    deltas: list[int] = []
    for position, value in enumerate(resolution_tick_deltas):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            or value > _UINT64_MAX
        ):
            raise Stage0StatisticsError(
                f"timer-resolution observation {position} must be a positive uint64 integer"
            )
        deltas.append(value)
    device_ticks: list[float] = []
    host_midpoints: list[float] = []
    host_half_intervals: list[float] = []
    for position, point in enumerate(points):
        if not isinstance(point, Mapping):
            raise Stage0StatisticsError(f"calibration point {position} must be a mapping")
        ordinal = _strict_int(
            point,
            "point_ordinal",
            position=position,
            minimum=0,
            maximum=_MAX_ORDINAL,
        )
        if ordinal != position:
            raise Stage0StatisticsError("calibration point ordinals must be contiguous and ordered")
        device = _strict_int(
            point,
            "device_ticks",
            position=position,
            minimum=0,
            maximum=_UINT64_MAX,
        )
        started = _strict_int(
            point,
            "host_started_monotonic_ns",
            position=position,
            minimum=0,
            maximum=_INT64_MAX,
        )
        finished = _strict_int(
            point,
            "host_finished_monotonic_ns",
            position=position,
            minimum=0,
            maximum=_INT64_MAX,
        )
        if finished <= started:
            raise Stage0StatisticsError(
                f"calibration point {position} requires a positive host interval"
            )
        if device_ticks and device <= device_ticks[-1]:
            raise Stage0StatisticsError("calibration device ticks must strictly increase")
        midpoint = started + (finished - started) / 2.0
        if host_midpoints and midpoint <= host_midpoints[-1]:
            raise Stage0StatisticsError("calibration host midpoints must strictly increase")
        try:
            device_value = float(device)
        except (OverflowError, ValueError) as exc:
            raise Stage0StatisticsError(
                f"calibration point {position} device_ticks is outside numeric range"
            ) from exc
        device_ticks.append(device_value)
        host_midpoints.append(midpoint)
        host_half_intervals.append((finished - started) / 2.0)

    mean_ticks = fmean(device_ticks)
    mean_host = fmean(host_midpoints)
    sum_squares = sum((value - mean_ticks) ** 2 for value in device_ticks)
    if sum_squares <= 0:
        raise Stage0StatisticsError("calibration device ticks have no variance")
    slope = sum(
        (device - mean_ticks) * (host - mean_host)
        for device, host in zip(device_ticks, host_midpoints, strict=True)
    ) / sum_squares
    if not math.isfinite(slope) or slope <= 0:
        raise Stage0StatisticsError("calibration ns_per_tick must be finite and positive")
    intercept = mean_host - slope * mean_ticks
    max_residual = max(
        abs(host - (intercept + slope * device)) + half_interval
        for device, host, half_interval in zip(
            device_ticks,
            host_midpoints,
            host_half_intervals,
            strict=True,
        )
    )
    try:
        resolution = min(deltas) * slope
    except (ArithmeticError, ValueError) as exc:
        raise Stage0StatisticsError("timer resolution is outside numeric range") from exc
    if not math.isfinite(resolution) or resolution <= 0:
        raise Stage0StatisticsError("timer resolution must be finite and positive")
    return ClockCalibrationStatistics(
        timer_resolution_ns=resolution,
        ns_per_tick=slope,
        max_residual_ns=max_residual,
        point_count=len(points),
    )


def normalize_device_samples(
    samples: Sequence[Mapping[str, Any]],
    *,
    ns_per_tick: float,
) -> tuple[NormalizedSample, ...]:
    """Validate and normalize device ticks into one elapsed-nanosecond value per iteration."""

    scale = _finite_float(ns_per_tick, name="ns_per_tick")
    if scale <= 0:
        raise Stage0StatisticsError("ns_per_tick must be greater than zero")
    if not samples:
        raise Stage0StatisticsError("at least one raw sample is required")

    normalized: list[NormalizedSample] = []
    seen_acquisitions: set[int] = set()
    previous_restart = -1
    for position, sample in enumerate(samples):
        if not isinstance(sample, Mapping):
            raise Stage0StatisticsError(f"sample {position} must be a mapping")
        restart = _strict_int(
            sample,
            "restart_ordinal",
            position=position,
            minimum=0,
            maximum=_MAX_ORDINAL,
        )
        acquisition = _strict_int(
            sample,
            "acquisition_ordinal",
            position=position,
            minimum=0,
            maximum=_MAX_ORDINAL,
        )
        started = _strict_int(
            sample,
            "started_device_ticks",
            position=position,
            minimum=0,
            maximum=_UINT64_MAX,
        )
        finished = _strict_int(
            sample,
            "finished_device_ticks",
            position=position,
            minimum=0,
            maximum=_UINT64_MAX,
        )
        batch_iterations = _strict_int(
            sample,
            "batch_iterations",
            position=position,
            minimum=1,
            maximum=_MAX_ORDINAL,
        )

        if acquisition in seen_acquisitions:
            raise Stage0StatisticsError(f"duplicate acquisition_ordinal {acquisition}")
        seen_acquisitions.add(acquisition)
        if acquisition != position:
            raise Stage0StatisticsError(
                "acquisition_ordinal must be contiguous, start at zero, and match file order"
            )
        if restart < previous_restart:
            raise Stage0StatisticsError("restart_ordinal groups must not be interleaved")
        previous_restart = restart
        if finished <= started:
            raise Stage0StatisticsError(
                f"sample {position} finished_device_ticks must exceed started_device_ticks"
            )

        try:
            sample_ns = (finished - started) * scale / batch_iterations
        except (ArithmeticError, ValueError) as exc:
            raise Stage0StatisticsError(
                f"sample {position} duration is outside numeric range"
            ) from exc
        if not math.isfinite(sample_ns) or sample_ns <= 0:
            raise Stage0StatisticsError(f"sample {position} has an invalid normalized duration")

        arm = _optional_text(sample, "arm", position=position)
        segment = _optional_text(sample, "segment", position=position)
        if (arm is None) != (segment is None):
            raise Stage0StatisticsError(f"sample {position} must provide both arm and segment")
        if arm is not None:
            arm = _canonical_arm(arm, position=position)
            segment = segment.upper()
            if segment in {"NOISE", "TIMER"}:
                if arm != "SINGLE":
                    raise Stage0StatisticsError(
                        f"sample {position} segment {segment!r} requires arm='single'"
                    )
            elif segment not in _SEGMENT_ARM:
                raise Stage0StatisticsError(
                    f"sample {position} has unknown ABBA segment {segment!r}"
                )
            elif arm != _SEGMENT_ARM[segment]:
                raise Stage0StatisticsError(
                    f"sample {position} arm {arm!r} does not match segment {segment!r}"
                )

        normalized.append(
            NormalizedSample(
                restart_ordinal=restart,
                acquisition_ordinal=acquisition,
                sample_ns=sample_ns,
                arm=arm,
                segment=segment,
            )
        )

    _validate_restart_ordinals(normalized)
    return tuple(normalized)


def mark_hampel_outliers(
    values: Sequence[float],
    *,
    threshold: float = 3.0,
) -> tuple[bool, ...]:
    """Return Hampel/MAD flags without removing or modifying any observation."""

    checked = _finite_values(values, name="values")
    limit = _finite_float(threshold, name="threshold")
    if limit <= 0:
        raise Stage0StatisticsError("threshold must be greater than zero")

    center = median(checked)
    mad = median(abs(value - center) for value in checked)
    if mad == 0:
        return tuple(value != center for value in checked)
    scaled_mad = 1.4826 * mad
    return tuple(abs(value - center) > limit * scaled_mad for value in checked)


def compute_noise_statistics(
    samples: Sequence[NormalizedSample],
    *,
    evidence_hash: str,
    alpha: float = 0.05,
    power: float = 0.80,
    bootstrap_iterations: int = 10_000,
    hampel_threshold: float = 3.0,
) -> NoiseStatistics:
    """Recompute restart-level mean, sample sigma/CV, MDE, and a deterministic mean CI."""

    checked = _validate_normalized_samples(samples, require_abba=False)
    alpha_value = _probability(alpha, name="alpha")
    power_value = _probability(power, name="power")
    if power_value <= 0.5:
        raise Stage0StatisticsError("power must be greater than 0.5")

    restart_groups = _restart_groups(checked)
    if len(restart_groups) < 2:
        raise Stage0StatisticsError("at least two independent restarts are required")
    restart_means = tuple(fmean(item.sample_ns for item in group) for group in restart_groups)
    overall_mean = fmean(restart_means)
    if not math.isfinite(overall_mean) or overall_mean <= 0:
        raise Stage0StatisticsError("restart mean must be finite and greater than zero")
    sigma = stdev(restart_means)
    cv = sigma / overall_mean
    normal = NormalDist()
    mde = (
        (normal.inv_cdf(1 - alpha_value / 2) + normal.inv_cdf(power_value))
        * sigma
        * math.sqrt(2 / len(restart_means))
        / overall_mean
    )
    ci_lower, ci_upper = bootstrap_mean_ci(
        restart_means,
        evidence_hash=evidence_hash,
        alpha=alpha_value,
        iterations=bootstrap_iterations,
        domain="noise-mean",
    )
    outliers = mark_hampel_outliers(
        [item.sample_ns for item in checked], threshold=hampel_threshold
    )
    return NoiseStatistics(
        mean_ns=overall_mean,
        sigma_ns=sigma,
        cv=cv,
        mde_fraction=mde,
        ci_lower_ns=ci_lower,
        ci_upper_ns=ci_upper,
        restart_means_ns=restart_means,
        outlier_count=sum(outliers),
        sample_count=len(checked),
    )


def compute_abba_effect(
    samples: Sequence[NormalizedSample],
    *,
    evidence_hash: str,
    alpha: float = 0.025,
    bootstrap_iterations: int = 10_000,
    hampel_threshold: float = 3.0,
) -> SignalStatistics:
    """Compute the paired slowdown ``(B - A) / A`` for strict A1-B1-B2-A2 restarts."""

    checked = _validate_normalized_samples(samples, require_abba=True)
    alpha_value = _probability(alpha, name="alpha")
    restart_groups = _restart_groups(checked)
    if len(restart_groups) < 2:
        raise Stage0StatisticsError("at least two independent restarts are required")

    baseline_means: list[float] = []
    comparison_means: list[float] = []
    effects: list[float] = []
    for group in restart_groups:
        segment_groups: dict[str, list[NormalizedSample]] = defaultdict(list)
        observed_blocks: list[str] = []
        for item in group:
            assert item.segment is not None
            segment_groups[item.segment].append(item)
            if not observed_blocks or observed_blocks[-1] != item.segment:
                observed_blocks.append(item.segment)
        if tuple(observed_blocks) != _ABBA_SEGMENTS:
            raise Stage0StatisticsError(
                f"restart {group[0].restart_ordinal} must follow A1-B1-B2-A2 acquisition order"
            )
        segment_sizes = {len(segment_groups[name]) for name in _ABBA_SEGMENTS}
        if len(segment_sizes) != 1:
            raise Stage0StatisticsError(
                f"restart {group[0].restart_ordinal} ABBA segments must have equal sample counts"
            )

        a_mean = fmean(item.sample_ns for name in ("A1", "A2") for item in segment_groups[name])
        b_mean = fmean(item.sample_ns for name in ("B1", "B2") for item in segment_groups[name])
        if not math.isfinite(a_mean) or a_mean <= 0:
            raise Stage0StatisticsError(
                "baseline restart mean must be finite and greater than zero"
            )
        effect = (b_mean - a_mean) / a_mean
        if not math.isfinite(effect):
            raise Stage0StatisticsError("restart effect must be finite")
        baseline_means.append(a_mean)
        comparison_means.append(b_mean)
        effects.append(effect)

    ci_lower, ci_upper = bootstrap_mean_ci(
        effects,
        evidence_hash=evidence_hash,
        alpha=alpha_value,
        iterations=bootstrap_iterations,
        domain="abba-effect",
    )
    outliers = mark_hampel_outliers(
        [item.sample_ns for item in checked], threshold=hampel_threshold
    )
    return SignalStatistics(
        effect_fraction=fmean(effects),
        ci_lower=ci_lower,
        ci_upper=ci_upper,
        restart_effects=tuple(effects),
        baseline_restart_means_ns=tuple(baseline_means),
        comparison_restart_means_ns=tuple(comparison_means),
        outlier_count=sum(outliers),
        sample_count=len(checked),
    )


def bootstrap_mean_ci(
    values: Sequence[float],
    *,
    evidence_hash: str,
    alpha: float,
    iterations: int = 10_000,
    domain: str = "mean",
) -> tuple[float, float]:
    """Return a deterministic percentile bootstrap confidence interval for a mean."""

    checked = _finite_values(values, name="values")
    alpha_value = _probability(alpha, name="alpha")
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 1:
        raise Stage0StatisticsError("iterations must be a positive integer")
    if not domain:
        raise Stage0StatisticsError("bootstrap domain must not be empty")

    rng = random.Random(derive_bootstrap_seed(evidence_hash, domain=domain))
    count = len(checked)
    bootstrapped = [
        fmean(checked[rng.randrange(count)] for _ in range(count)) for _ in range(iterations)
    ]
    bootstrapped.sort()
    return (
        _percentile(bootstrapped, alpha_value / 2),
        _percentile(bootstrapped, 1 - alpha_value / 2),
    )


def derive_bootstrap_seed(evidence_hash: str, *, domain: str) -> int:
    match = _SHA256_RE.fullmatch(evidence_hash)
    if match is None:
        raise Stage0StatisticsError("evidence_hash must be a lowercase sha256 digest")
    if not domain:
        raise Stage0StatisticsError("bootstrap domain must not be empty")
    material = bytes.fromhex(match.group(1)) + b"\x00" + domain.encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest(), byteorder="big")


def bonferroni_alpha(*, familywise_alpha: float = 0.05, comparisons: int = 2) -> float:
    alpha = _probability(familywise_alpha, name="familywise_alpha")
    if isinstance(comparisons, bool) or not isinstance(comparisons, int) or comparisons < 1:
        raise Stage0StatisticsError("comparisons must be a positive integer")
    return alpha / comparisons


def known_signal_passes(
    result: SignalStatistics,
    *,
    minimum_effect: float = 0.10,
    confidence_floor: float = 0.03,
) -> bool:
    minimum = _non_negative_finite(minimum_effect, name="minimum_effect")
    floor = _non_negative_finite(confidence_floor, name="confidence_floor")
    return result.effect_fraction >= minimum and result.ci_lower > floor


def null_signal_passes(
    result: SignalStatistics,
    *,
    equivalence_margin: float = 0.03,
) -> bool:
    margin = _non_negative_finite(equivalence_margin, name="equivalence_margin")
    return result.ci_lower >= -margin and result.ci_upper <= margin


def with_hampel_flags(
    samples: Sequence[NormalizedSample],
    *,
    threshold: float = 3.0,
) -> tuple[NormalizedSample, ...]:
    """Return a flagged copy; all samples remain present and numerically unchanged."""

    checked = _validate_normalized_samples(samples, require_abba=False)
    flags = mark_hampel_outliers([item.sample_ns for item in checked], threshold=threshold)
    return tuple(replace(item, outlier=flag) for item, flag in zip(checked, flags, strict=True))


def _validate_normalized_samples(
    samples: Sequence[NormalizedSample],
    *,
    require_abba: bool,
) -> tuple[NormalizedSample, ...]:
    if not samples:
        raise Stage0StatisticsError("at least one normalized sample is required")
    checked: list[NormalizedSample] = []
    seen: set[int] = set()
    previous_restart = -1
    for position, sample in enumerate(samples):
        if not isinstance(sample, NormalizedSample):
            raise Stage0StatisticsError(f"sample {position} must be a NormalizedSample")
        if (
            isinstance(sample.restart_ordinal, bool)
            or not isinstance(sample.restart_ordinal, int)
            or sample.restart_ordinal < 0
        ):
            raise Stage0StatisticsError(f"sample {position} has invalid restart_ordinal")
        if (
            isinstance(sample.acquisition_ordinal, bool)
            or not isinstance(sample.acquisition_ordinal, int)
            or sample.acquisition_ordinal < 0
        ):
            raise Stage0StatisticsError(f"sample {position} has invalid acquisition_ordinal")
        if sample.acquisition_ordinal in seen:
            raise Stage0StatisticsError(
                f"duplicate acquisition_ordinal {sample.acquisition_ordinal}"
            )
        seen.add(sample.acquisition_ordinal)
        if sample.acquisition_ordinal != position:
            raise Stage0StatisticsError(
                "acquisition_ordinal must be contiguous, start at zero, and match file order"
            )
        if sample.restart_ordinal < previous_restart:
            raise Stage0StatisticsError("restart_ordinal groups must not be interleaved")
        previous_restart = sample.restart_ordinal
        if not math.isfinite(sample.sample_ns) or sample.sample_ns <= 0:
            raise Stage0StatisticsError(f"sample {position} sample_ns must be finite and positive")
        if require_abba and (sample.arm is None or sample.segment is None):
            raise Stage0StatisticsError(f"sample {position} is missing ABBA arm or segment")
        if require_abba and (
            sample.arm not in {"A", "B"}
            or sample.segment not in _SEGMENT_ARM
            or _SEGMENT_ARM[sample.segment] != sample.arm
        ):
            raise Stage0StatisticsError(f"sample {position} has inconsistent ABBA metadata")
        checked.append(sample)
    _validate_restart_ordinals(checked)
    return tuple(checked)


def _validate_restart_ordinals(samples: Sequence[NormalizedSample]) -> None:
    ordinals = sorted({item.restart_ordinal for item in samples})
    if ordinals != list(range(len(ordinals))):
        raise Stage0StatisticsError("restart_ordinal must be contiguous and start at zero")


def _restart_groups(
    samples: Sequence[NormalizedSample],
) -> tuple[tuple[NormalizedSample, ...], ...]:
    grouped: dict[int, list[NormalizedSample]] = defaultdict(list)
    for sample in samples:
        grouped[sample.restart_ordinal].append(sample)
    return tuple(tuple(grouped[index]) for index in range(len(grouped)))


def _strict_int(
    source: Mapping[str, Any],
    key: str,
    *,
    position: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if key not in source:
        raise Stage0StatisticsError(f"sample {position} is missing {key}")
    value = source[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise Stage0StatisticsError(f"sample {position} {key} must be an integer")
    if value < minimum:
        raise Stage0StatisticsError(f"sample {position} {key} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise Stage0StatisticsError(f"sample {position} {key} must be at most {maximum}")
    return value


def _optional_text(source: Mapping[str, Any], key: str, *, position: int) -> str | None:
    value = source.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise Stage0StatisticsError(f"sample {position} {key} must be non-empty text")
    return value.strip()


def _canonical_arm(value: str, *, position: int) -> str:
    normalized = value.upper()
    if normalized == "SINGLE":
        return normalized
    if normalized in {"A", "BASELINE"}:
        return "A"
    if normalized in {"B", "COMPARISON"}:
        return "B"
    raise Stage0StatisticsError(f"sample {position} has unknown ABBA arm {value!r}")


def _finite_values(values: Sequence[float], *, name: str) -> tuple[float, ...]:
    if not values:
        raise Stage0StatisticsError(f"{name} must not be empty")
    return tuple(
        _finite_float(value, name=f"{name}[{index}]") for index, value in enumerate(values)
    )


def _finite_float(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Stage0StatisticsError(f"{name} must be numeric")
    try:
        converted = float(value)
    except (OverflowError, ValueError) as exc:
        raise Stage0StatisticsError(f"{name} is outside numeric range") from exc
    if not math.isfinite(converted):
        raise Stage0StatisticsError(f"{name} must be finite")
    return converted


def _non_negative_finite(value: float, *, name: str) -> float:
    converted = _finite_float(value, name=name)
    if converted < 0:
        raise Stage0StatisticsError(f"{name} must not be negative")
    return converted


def _probability(value: float, *, name: str) -> float:
    converted = _finite_float(value, name=name)
    if not 0 < converted < 1:
        raise Stage0StatisticsError(f"{name} must be between zero and one")
    return converted


def _percentile(sorted_values: Sequence[float], quantile: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * quantile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return sorted_values[lower_index]
    weight = position - lower_index
    return sorted_values[lower_index] * (1 - weight) + sorted_values[upper_index] * weight
