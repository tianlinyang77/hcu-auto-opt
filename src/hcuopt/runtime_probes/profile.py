from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import ArtifactManifest, MountSpec, SourceSnapshot


def _absolute_path(value: str, field_name: str) -> str:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field_name} must be a clean absolute POSIX path")
    return value


class ProfilerToolConfiguration(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=100)
    version_argv: tuple[str, ...] = Field(min_length=1)
    profile_argv: tuple[str, ...] = Field(min_length=1)
    output_format: Literal["json", "jsonl", "csv", "torch_trace"]
    output_path: str | None = None
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
        return None if value is None else _absolute_path(value, "triage_work_dir")

    @field_validator("mounts")
    @classmethod
    def require_read_only_mounts(
        cls, mounts: tuple[MountSpec, ...]
    ) -> tuple[MountSpec, ...]:
        if any(not mount.read_only for mount in mounts):
            raise ValueError("profiler profile mounts must be read-only")
        return mounts


class OverlayPhaseConfiguration(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    argv: tuple[str, ...] = Field(min_length=1)
    working_directory: str
    environment: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=600, ge=1, le=86_400)
    mounts: tuple[MountSpec, ...] = ()

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

    @field_validator("mounts")
    @classmethod
    def require_read_only_mounts(
        cls, mounts: tuple[MountSpec, ...]
    ) -> tuple[MountSpec, ...]:
        if any(not mount.read_only for mount in mounts):
            raise ValueError("overlay profile mounts must be read-only")
        return mounts


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


class RuntimeProbeProfile(ContractModel):
    """Deployment-owned S0-C configuration; it is never accepted from run creators."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    profile: str = Field(min_length=1, max_length=200)
    target_id: str = Field(min_length=1, max_length=128)
    target_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    profiler: ProfilerProbeConfiguration
    hotpatch: OverlayProbeConfiguration
