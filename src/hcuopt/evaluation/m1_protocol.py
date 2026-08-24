from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.measurement.evidence import canonical_json_bytes

M1_CORRECTNESS_PROTOCOL_VERSION = "m1-kernel-correctness-v1"
M1_CORRECTNESS_EVIDENCE_VERSION = "m1-kernel-correctness-evidence-v1"
MAX_PROTOCOL_BYTES = 1024 * 1024

_REGISTERED_PROTOCOL_FILES = {
    M1_CORRECTNESS_PROTOCOL_VERSION: "m1-kernel-correctness-v1.yaml",
}
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class M1ProtocolError(ValueError):
    """The requested M1 protocol or Hotspot specification is invalid."""


class _M1ProtocolModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class M1KernelCorrectnessProtocol(_M1ProtocolModel):
    protocol_version: Literal["m1-kernel-correctness-v1"]
    schema_version: Literal["m1-kernel-correctness-protocol-v1"]
    max_output_elements: int = Field(ge=1, le=10_000_000)
    max_evidence_bytes: int = Field(ge=1024, le=256 * 1024 * 1024)
    max_execution_seconds: int = Field(ge=1, le=3600)
    min_repeats: int = Field(ge=1, le=100)
    max_repeats: int = Field(ge=1, le=1000)
    bootstrap_resamples: int = Field(ge=1000, le=1_000_000)
    confidence_level: float = Field(gt=0.5, lt=1.0)
    bootstrap_method: Literal["percentile"]
    bootstrap_seed_source: Literal["input_evidence_sha256"]
    large_value_min_abs: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_repeat_bounds(self) -> M1KernelCorrectnessProtocol:
        if self.max_repeats < self.min_repeats:
            raise ValueError("max_repeats cannot be smaller than min_repeats")
        return self


class M1TensorSpec(_M1ProtocolModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
    shape: tuple[int, ...] = Field(min_length=1, max_length=16)
    dtype: Literal[
        "bool",
        "int8",
        "int16",
        "int32",
        "int64",
        "float16",
        "bfloat16",
        "float32",
        "float64",
    ]

    @field_validator("shape", mode="before")
    @classmethod
    def freeze_shape(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("shape")
    @classmethod
    def validate_shape(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(isinstance(item, bool) or item < 1 for item in value):
            raise ValueError("tensor dimensions must be positive integers")
        return value

    @property
    def element_count(self) -> int:
        result = 1
        for dimension in self.shape:
            result *= dimension
        return result


class M1OutputSpec(M1TensorSpec):
    atol: float = Field(ge=0)
    rtol: float = Field(ge=0)
    equal_nan: bool

    @model_validator(mode="after")
    def require_exact_integer_comparison(self) -> M1OutputSpec:
        if self.dtype.startswith("int") or self.dtype == "bool":
            if self.atol != 0 or self.rtol != 0 or self.equal_nan:
                raise ValueError("integer and bool outputs require exact comparison")
        return self


class M1CorrectnessCase(_M1ProtocolModel):
    case_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    inputs: tuple[M1TensorSpec, ...] = Field(min_length=1)
    outputs: tuple[M1OutputSpec, ...] = Field(min_length=1)
    seeds: tuple[int, ...] = Field(min_length=1, max_length=100)
    special_values: tuple[
        Literal["ordinary", "zero", "negative", "large", "nan", "positive_inf", "negative_inf"],
        ...,
    ] = Field(min_length=1)
    repeats: int = Field(ge=1, le=1000)

    @field_validator("inputs", "outputs", "seeds", "special_values", mode="before")
    @classmethod
    def freeze_sequences(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_case_identity(self) -> M1CorrectnessCase:
        if len(self.seeds) != len(set(self.seeds)):
            raise ValueError("case seeds must be unique")
        if len(self.special_values) != len(set(self.special_values)):
            raise ValueError("case special values must be unique")
        for values, label in ((self.inputs, "input"), (self.outputs, "output")):
            names = [item.name for item in values]
            if len(names) != len(set(names)):
                raise ValueError(f"{label} tensor names must be unique")
        has_non_floating = any(
            item.dtype == "bool" or item.dtype.startswith("int") for item in self.inputs
        )
        if has_non_floating and any(
            value in {"nan", "positive_inf", "negative_inf"} for value in self.special_values
        ):
            raise ValueError("non-finite special values require floating-point inputs")
        return self


class M1InputExpectation(_M1ProtocolModel):
    case_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    seed: int
    special_value: Literal[
        "ordinary",
        "zero",
        "negative",
        "large",
        "nan",
        "positive_inf",
        "negative_inf",
    ]
    input_hash: str = Field(pattern=SHA256_PATTERN)


class M1HotspotCorrectnessSpec(_M1ProtocolModel):
    schema_version: Literal["m1-hotspot-correctness-spec-v1"] = "m1-hotspot-correctness-spec-v1"
    hotspot_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
    reference_implementation: str = Field(min_length=1, max_length=1000)
    reference_source_hash: str = Field(pattern=SHA256_PATTERN)
    cases: tuple[M1CorrectnessCase, ...] = Field(min_length=1, max_length=100)
    input_expectations: tuple[M1InputExpectation, ...] = Field(min_length=1)

    @field_validator("cases", "input_expectations", mode="before")
    @classmethod
    def freeze_cases(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_unique_cases(self) -> M1HotspotCorrectnessSpec:
        case_ids = [item.case_id for item in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Hotspot correctness case IDs must be unique")
        expected = {
            (case.case_id, seed, special)
            for case in self.cases
            for seed in case.seeds
            for special in case.special_values
        }
        actual = {(item.case_id, item.seed, item.special_value) for item in self.input_expectations}
        if len(actual) != len(self.input_expectations):
            raise ValueError("Hotspot input expectations must be unique")
        if actual != expected:
            raise ValueError("Hotspot input expectations must cover every case/seed/special value")
        return self


@dataclass(frozen=True, slots=True)
class LoadedM1Protocol:
    protocol: M1KernelCorrectnessProtocol
    protocol_hash: str
    canonical_bytes: bytes
    source_path: Path


def canonical_m1_protocol_bytes(protocol: M1KernelCorrectnessProtocol) -> bytes:
    return canonical_json_bytes(protocol)


def m1_protocol_sha256(protocol: M1KernelCorrectnessProtocol) -> str:
    return "sha256:" + hashlib.sha256(canonical_m1_protocol_bytes(protocol)).hexdigest()


def m1_hotspot_spec_sha256(spec: M1HotspotCorrectnessSpec) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(spec)).hexdigest()


def load_m1_protocol(path: Path, *, expected_version: str | None = None) -> LoadedM1Protocol:
    source_path = path.absolute()
    if source_path.is_symlink():
        raise M1ProtocolError("M1 protocol file cannot be a symbolic link")
    try:
        metadata = source_path.stat()
    except OSError as exc:
        raise M1ProtocolError(f"cannot read M1 protocol: {source_path}") from exc
    if not source_path.is_file():
        raise M1ProtocolError(f"M1 protocol is not a regular file: {source_path}")
    if metadata.st_size > MAX_PROTOCOL_BYTES:
        raise M1ProtocolError("M1 protocol exceeds the 1 MiB size limit")
    try:
        raw: Any = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise M1ProtocolError(f"invalid M1 protocol YAML: {source_path}") from exc
    if not isinstance(raw, dict):
        raise M1ProtocolError("M1 protocol root must be a mapping")
    try:
        protocol = M1KernelCorrectnessProtocol.model_validate(raw)
    except ValidationError as exc:
        raise M1ProtocolError(f"invalid M1 protocol: {exc}") from exc
    if expected_version is not None and protocol.protocol_version != expected_version:
        raise M1ProtocolError(
            "M1 protocol version does not match the registered file: "
            f"expected {expected_version}, got {protocol.protocol_version}"
        )
    encoded = canonical_m1_protocol_bytes(protocol)
    return LoadedM1Protocol(
        protocol=protocol,
        protocol_hash="sha256:" + hashlib.sha256(encoded).hexdigest(),
        canonical_bytes=encoded,
        source_path=source_path.resolve(),
    )


def load_registered_m1_protocol(
    version: str = M1_CORRECTNESS_PROTOCOL_VERSION,
    *,
    config_root: Path | None = None,
) -> LoadedM1Protocol:
    if _VERSION_PATTERN.fullmatch(version) is None:
        raise M1ProtocolError(f"invalid M1 protocol version: {version!r}")
    filename = _REGISTERED_PROTOCOL_FILES.get(version)
    if filename is None:
        raise M1ProtocolError(f"unregistered M1 protocol version: {version}")
    root = config_root or Path(__file__).resolve().parent / "protocols"
    return load_m1_protocol(root / filename, expected_version=version)
