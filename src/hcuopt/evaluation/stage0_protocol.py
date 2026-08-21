from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.measurement.evidence import canonical_json_bytes

STAGE0_PROTOCOL_VERSION = "s0-g0-v1"
MEASUREMENT_EVIDENCE_SCHEMA_VERSION = "measurement-evidence-v2"
MAX_PROTOCOL_BYTES = 1024 * 1024

_REGISTERED_PROTOCOL_FILES = {
    "s0-g0-v1": "s0-g0-v1.yaml",
    "s0-g0-v2": "s0-g0-v2.yaml",
}
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class Stage0ProtocolError(ValueError):
    """The requested Stage 0 protocol is missing, unregistered, or malformed."""


class _ProtocolModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Stage0SamplingProtocol(_ProtocolModel):
    restart_count: int = Field(ge=2)
    warmup_count: int = Field(ge=0)
    batch_iterations: int = Field(ge=1)
    noise_samples_per_restart: int = Field(ge=2)
    signal_segment_order: tuple[Literal["A1", "B1", "B2", "A2"], ...]
    signal_samples_per_segment: int = Field(ge=2)

    @field_validator("signal_segment_order", mode="before")
    @classmethod
    def freeze_signal_segment_order(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def require_abba_order(self) -> Stage0SamplingProtocol:
        if self.signal_segment_order != ("A1", "B1", "B2", "A2"):
            raise ValueError("signal_segment_order must be A1,B1,B2,A2")
        return self


class Stage0StatisticalMethod(_ProtocolModel):
    alpha: float = Field(gt=0, lt=1)
    power: float = Field(gt=0, lt=1)
    bootstrap_resamples: int = Field(ge=1000)
    bootstrap_method: Literal["percentile"]
    bootstrap_seed_source: Literal["input_evidence_sha256"]
    multiple_comparison_method: Literal["bonferroni"]
    corrected_signal_alpha: float = Field(gt=0, lt=1)

    @model_validator(mode="after")
    def validate_bonferroni_alpha(self) -> Stage0StatisticalMethod:
        if abs(self.corrected_signal_alpha - self.alpha / 2.0) > 1e-12:
            raise ValueError("corrected_signal_alpha must equal alpha/2 for two signal gates")
        return self


class Stage0MeasurementGates(_ProtocolModel):
    max_cv_ratio: float = Field(gt=0, lt=1)
    max_mde_ratio: float = Field(gt=0, lt=1)
    known_signal_direction: Literal["slowdown"]
    known_signal_min_effect_ratio: float = Field(gt=0, lt=1)
    known_signal_ci_floor_ratio: float = Field(gt=0, lt=1)
    null_equivalence_margin_ratio: float = Field(gt=0, lt=1)

    @model_validator(mode="after")
    def validate_signal_thresholds(self) -> Stage0MeasurementGates:
        if self.known_signal_min_effect_ratio <= self.known_signal_ci_floor_ratio:
            raise ValueError("known signal effect must exceed its confidence-interval floor")
        return self


class Stage0EnvironmentGates(_ProtocolModel):
    max_temperature_c: float = Field(gt=0)
    max_temperature_delta_c: float = Field(gt=0)
    clock_tolerance_ratio: float = Field(gt=0, lt=1)
    required_performance_level: Literal["manual"]
    power_gate_enabled: Literal[False]


class Stage0TimerGates(_ProtocolModel):
    comparison_basis: Literal["normalized_iteration", "raw_batch_interval"] = (
        "normalized_iteration"
    )
    max_resolution_to_mean_ratio: float = Field(gt=0, lt=1)
    max_residual_to_mean_ratio: float = Field(gt=0, lt=1)


class Stage0OutlierProtocol(_ProtocolModel):
    method: Literal["hampel_mad"]
    hampel_sigma: float = Field(gt=0)
    remove_flagged_samples: Literal[False]
    max_flagged_ratio: float = Field(ge=0, lt=1)


class Stage0StatisticsProtocol(_ProtocolModel):
    protocol_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    raw_evidence_schema_version: Literal["measurement-evidence-v2"]
    timing_metric_name: Literal["kernel_elapsed"]
    timing_unit: Literal["ns"]
    sampling: Stage0SamplingProtocol
    statistics: Stage0StatisticalMethod
    measurement_gates: Stage0MeasurementGates
    environment_gates: Stage0EnvironmentGates
    timer_gates: Stage0TimerGates
    outliers: Stage0OutlierProtocol

    @property
    def alpha(self) -> float:
        return self.statistics.alpha

    @property
    def power(self) -> float:
        return self.statistics.power

    @property
    def bootstrap_resamples(self) -> int:
        return self.statistics.bootstrap_resamples

    @property
    def max_cv_ratio(self) -> float:
        return self.measurement_gates.max_cv_ratio

    @property
    def max_mde_ratio(self) -> float:
        return self.measurement_gates.max_mde_ratio


@dataclass(frozen=True, slots=True)
class LoadedStage0Protocol:
    protocol: Stage0StatisticsProtocol
    protocol_hash: str
    canonical_bytes: bytes
    source_path: Path

    @property
    def sha256(self) -> str:
        return self.protocol_hash


def canonical_protocol_bytes(protocol: Stage0StatisticsProtocol) -> bytes:
    """Return the only byte representation used to identify a protocol."""

    return canonical_json_bytes(protocol)


def protocol_sha256(protocol: Stage0StatisticsProtocol) -> str:
    return "sha256:" + hashlib.sha256(canonical_protocol_bytes(protocol)).hexdigest()


def load_stage0_protocol(
    path: Path,
    *,
    expected_version: str | None = None,
) -> LoadedStage0Protocol:
    """Load strict YAML, then hash the canonical validated model rather than YAML layout."""

    source_path = path.absolute()
    if source_path.is_symlink():
        raise Stage0ProtocolError("Stage 0 protocol file cannot be a symbolic link")
    try:
        stat = source_path.stat()
    except OSError as exc:
        raise Stage0ProtocolError(f"cannot read Stage 0 protocol: {source_path}") from exc
    if not source_path.is_file():
        raise Stage0ProtocolError(f"Stage 0 protocol is not a regular file: {source_path}")
    if stat.st_size > MAX_PROTOCOL_BYTES:
        raise Stage0ProtocolError("Stage 0 protocol exceeds the 1 MiB size limit")

    try:
        raw: Any = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise Stage0ProtocolError(f"invalid Stage 0 protocol YAML: {source_path}") from exc
    if not isinstance(raw, dict):
        raise Stage0ProtocolError("Stage 0 protocol root must be a mapping")
    try:
        protocol = Stage0StatisticsProtocol.model_validate(raw)
    except ValidationError as exc:
        raise Stage0ProtocolError(f"invalid Stage 0 protocol: {exc}") from exc
    if expected_version is not None and protocol.protocol_version != expected_version:
        raise Stage0ProtocolError(
            "Stage 0 protocol version does not match the registered file: "
            f"expected {expected_version}, got {protocol.protocol_version}"
        )
    encoded = canonical_protocol_bytes(protocol)
    return LoadedStage0Protocol(
        protocol=protocol,
        protocol_hash="sha256:" + hashlib.sha256(encoded).hexdigest(),
        canonical_bytes=encoded,
        source_path=source_path.resolve(),
    )


def load_registered_stage0_protocol(
    version: str,
    *,
    config_root: Path | None = None,
) -> LoadedStage0Protocol:
    """Resolve only a repository-registered protocol; callers cannot inject thresholds."""

    if _VERSION_PATTERN.fullmatch(version) is None:
        raise Stage0ProtocolError(f"invalid Stage 0 protocol version: {version!r}")
    filename = _REGISTERED_PROTOCOL_FILES.get(version)
    if filename is None:
        raise Stage0ProtocolError(f"unregistered Stage 0 protocol version: {version}")
    root = config_root or Path(__file__).resolve().parent / "protocols"
    return load_stage0_protocol(root / filename, expected_version=version)
