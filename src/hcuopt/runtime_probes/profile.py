from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import ArtifactManifest, MountSpec, SourceSnapshot


def _absolute_path(value: str, field_name: str) -> str:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field_name} must be a clean absolute POSIX path")
    return value


def _host_absolute_path(value: str, field_name: str) -> str:
    if not Path(value).is_absolute() and not PurePosixPath(value).is_absolute():
        raise ValueError(f"{field_name} must be an absolute host path")
    return value


class ProfilerToolConfiguration(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=100)
    version_argv: tuple[str, ...] = Field(min_length=1)
    profile_argv: tuple[str, ...] = Field(min_length=1)
    output_format: Literal["json", "jsonl", "csv", "torch_trace"]
    output_path: str | None = None
    output_host_uri: str | None = None
    timeout_seconds: int = Field(default=300, ge=1, le=86_400)

    @field_validator("version_argv", "profile_argv")
    @classmethod
    def validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value):
            raise ValueError("profiler argv entries cannot be empty")
        return value

    @model_validator(mode="after")
    def validate_output_path(self) -> ProfilerToolConfiguration:
        if self.output_format == "torch_trace" and self.output_path is None:
            raise ValueError("torch_trace profiler requires output_path")
        if self.output_path is not None:
            _absolute_path(self.output_path, "output_path")
        if self.output_host_uri is not None and not self.output_host_uri.startswith("file:"):
            raise ValueError("output_host_uri must be a file URI")
        if self.output_format != "torch_trace" and self.output_host_uri is not None:
            raise ValueError("output_host_uri is only valid for torch_trace")
        return self


class ProfilerProbeConfiguration(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_candidates: tuple[ProfilerToolConfiguration, ...] = Field(min_length=1)
    profile_workload: Literal["both", "prefill", "decode"] = "both"
    warmup_steps: int = Field(default=10, ge=1, le=10_000)
    num_steps: int = Field(default=5, ge=1, le=10_000)
    prefill_input_len: int = Field(default=4090, ge=1)
    prefill_output_len: int = Field(default=1, ge=1)
    decode_input_len: int = Field(default=1, ge=1)
    decode_output_len: int = Field(default=2048, ge=1)
    working_directory: str = "/workspace"
    environment: dict[str, str] = Field(default_factory=dict)
    mounts: tuple[MountSpec, ...] = ()
    triage_work_dir: str | None = None

    @field_validator("working_directory")
    @classmethod
    def validate_working_directory(cls, value: str) -> str:
        return _absolute_path(value, "working_directory")

    @field_validator("triage_work_dir")
    @classmethod
    def validate_triage_work_dir(cls, value: str | None) -> str | None:
        return None if value is None else _host_absolute_path(value, "triage_work_dir")

    @model_validator(mode="after")
    def validate_writable_output_mounts(self) -> ProfilerProbeConfiguration:
        if any(not mount.read_only for mount in self.mounts) and not any(
            candidate.output_host_uri is not None for candidate in self.tool_candidates
        ):
            raise ValueError("profiler writable mounts require a worker-owned output_host_uri")
        return self


class OverlayPhaseConfiguration(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    argv: tuple[str, ...] = Field(min_length=1)
    working_directory: str
    environment: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=600, ge=1, le=86_400)
    mounts: tuple[MountSpec, ...] = ()
    evidence_directory_uri: str | None = None
    implementation_source_uri: str | None = None

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value):
            raise ValueError("overlay argv entries cannot be empty")
        return value

    @field_validator("working_directory")
    @classmethod
    def validate_working_directory(cls, value: str) -> str:
        return _absolute_path(value, "working_directory")

    @model_validator(mode="after")
    def validate_formal_evidence_inputs(self) -> OverlayPhaseConfiguration:
        formal_values = (self.evidence_directory_uri, self.implementation_source_uri)
        if any(value is not None for value in formal_values) and any(
            value is None for value in formal_values
        ):
            raise ValueError("overlay phase Formal evidence URI fields must be supplied together")
        for value in formal_values:
            if value is not None and not value.startswith("file:"):
                raise ValueError("overlay Formal evidence inputs must be file URIs")
        if self.evidence_directory_uri is None and any(
            not mount.read_only for mount in self.mounts
        ):
            raise ValueError(
                "overlay writable mounts require an explicit Formal evidence directory"
            )
        return self


class OverlayProbeConfiguration(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workload_kind: Literal["generic_artifact_mount", "sglang_python_triton"]
    replacement_point: str = Field(min_length=1, max_length=500)
    baseline_source: SourceSnapshot
    candidate_source: SourceSnapshot
    artifact: ArtifactManifest
    overlay_mount_target: str
    activation_marker: str = Field(min_length=1, max_length=200)
    baseline: OverlayPhaseConfiguration
    candidate: OverlayPhaseConfiguration
    recovery: OverlayPhaseConfiguration

    @field_validator("overlay_mount_target")
    @classmethod
    def validate_overlay_mount_target(cls, value: str) -> str:
        return _absolute_path(value, "overlay_mount_target")

    @model_validator(mode="after")
    def validate_formal_phase_inputs(self) -> OverlayProbeConfiguration:
        phases = (self.baseline, self.candidate, self.recovery)
        evidence_directories = [phase.evidence_directory_uri for phase in phases]
        if any(value is not None for value in evidence_directories):
            if any(value is None for value in evidence_directories):
                raise ValueError("all overlay phases must declare Formal evidence directories")
            if len(set(evidence_directories)) != 3:
                raise ValueError("overlay Formal phase evidence directories must be distinct")
            if self.candidate.implementation_source_uri != self.artifact.uri:
                raise ValueError(
                    "candidate Formal implementation source must be the frozen Artifact"
                )
            if self.baseline.implementation_source_uri != self.recovery.implementation_source_uri:
                raise ValueError("baseline and recovery Formal implementation sources must match")
        return self


class RuntimeProbeProfile(ContractModel):
    """Deployment-owned S0-C configuration; it is never accepted from run creators."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    profile: str = Field(min_length=1, max_length=200)
    target_id: str = Field(min_length=1, max_length=128)
    target_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    profiler: ProfilerProbeConfiguration
    hotpatch: OverlayProbeConfiguration
