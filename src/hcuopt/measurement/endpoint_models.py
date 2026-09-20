# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Strict raw-evidence contracts for an M1 SGLang endpoint validation run."""

from __future__ import annotations

import hashlib
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from hcuopt.contracts.platform_v1 import GIT_COMMIT_PATTERN, SHA256_PATTERN
from hcuopt.domain.enums import LeaseScope
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.measurement.models import (
    ProcessLifecycleRecordV2,
    RawEvidenceFileV2,
    Stage0AdapterProvenance,
    Stage0LeaseBinding,
    StrictMeasurementModel,
)

EndpointArm = Literal["baseline", "candidate"]
EndpointRunMode = Literal["provisional", "formal"]


class SignedM1EvidenceReference(StrictMeasurementModel):
    """Immutable identity of the signed M1 result; endpoint runs never rewrite it."""

    task_id: UUID
    candidate_id: UUID
    baseline_epoch_id: UUID
    target_snapshot_id: UUID
    target_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    target_fingerprint: str = Field(pattern=SHA256_PATTERN)
    candidate_source_hash: str = Field(pattern=SHA256_PATTERN)
    artifact_id: UUID
    artifact_hash: str = Field(pattern=SHA256_PATTERN)
    evidence_bundle_id: UUID
    evidence_bundle_hash: str = Field(pattern=SHA256_PATTERN)
    signoff_id: UUID
    task_state: Literal["completed"] = "completed"
    candidate_state: Literal["accepted"] = "accepted"
    automatic_release_allowed: Literal[False] = False


class EndpointWorkloadSpec(StrictMeasurementModel):
    schema_version: Literal["sglang-endpoint-workload-v1"] = "sglang-endpoint-workload-v1"
    workload_id: str = Field(min_length=1, max_length=200)
    workload_hash: str = Field(pattern=SHA256_PATTERN)
    target_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    framework_version: str = Field(min_length=1, max_length=200)
    source_commit: str = Field(pattern=GIT_COMMIT_PATTERN)
    image_digest: str = Field(pattern=SHA256_PATTERN)
    model_path: str = Field(pattern=r"^/[^\x00]*$")
    served_model_name: str = Field(min_length=1, max_length=200)
    endpoint_path: Literal["/generate"] = "/generate"
    tensor_parallel_size: Literal[1] = 1
    closed_loop_concurrency: Literal[1] = 1
    attention_backend: str = Field(min_length=1, max_length=100)
    page_size: int = Field(ge=1, le=1_048_576)
    prompt: str = Field(min_length=1, max_length=1_000_000)
    prompt_sha256: str = Field(pattern=SHA256_PATTERN)
    expected_prompt_tokens: int = Field(ge=1, le=1_000_000)
    expected_completion_tokens: int = Field(ge=1, le=1_000_000)
    temperature: Literal[0.0] = 0.0
    sampling_seed: int = Field(ge=0, le=(1 << 63) - 1)
    ignore_eos: Literal[True] = True
    stream: bool
    cache_policy: Literal["fresh_namespace_per_acquisition"] = (
        "fresh_namespace_per_acquisition"
    )
    cpu_affinity: str = Field(min_length=1, max_length=200)
    numa_node: int = Field(ge=0, le=4096)

    @model_validator(mode="after")
    def bind_prompt_hash(self) -> EndpointWorkloadSpec:
        actual = "sha256:" + hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()
        if self.prompt_sha256 != actual:
            raise ValueError("endpoint prompt hash differs from the frozen prompt")
        return self


class EndpointMeasurementPlan(StrictMeasurementModel):
    protocol_version: Literal["m1-sglang-endpoint-v1"] = "m1-sglang-endpoint-v1"
    run_mode: EndpointRunMode
    acquisition_order: tuple[EndpointArm, ...]
    warmup_requests: int = Field(ge=1, le=10_000)
    measured_requests_per_acquisition: int = Field(ge=1, le=100_000)
    ready_timeout_seconds: int = Field(ge=1, le=3600)
    request_timeout_seconds: int = Field(ge=1, le=600)

    @field_validator("acquisition_order", mode="before")
    @classmethod
    def freeze_order(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_complete_abba(self) -> EndpointMeasurementPlan:
        if len(self.acquisition_order) < 4 or len(self.acquisition_order) % 4:
            raise ValueError("endpoint plan requires complete ABBA groups")
        for offset in range(0, len(self.acquisition_order), 4):
            if self.acquisition_order[offset : offset + 4] != (
                "baseline",
                "candidate",
                "candidate",
                "baseline",
            ):
                raise ValueError("endpoint plan requires Baseline-Candidate-Candidate-Baseline")
        return self


class EndpointActivationEvidence(StrictMeasurementModel):
    arm: EndpointArm
    image_digest: str = Field(pattern=SHA256_PATTERN)
    activation_mode: Literal["baseline", "startup_overlay"]
    loaded_artifact_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    import_attestation: RawEvidenceFileV2 | None = None
    cache_namespace_hash: str = Field(pattern=SHA256_PATTERN)
    cache_namespace_evidence: RawEvidenceFileV2
    cache_empty_before_start: Literal[True] = True

    @model_validator(mode="after")
    def bind_arm(self) -> EndpointActivationEvidence:
        if self.arm == "baseline":
            if (
                self.activation_mode != "baseline"
                or self.loaded_artifact_hash is not None
                or self.import_attestation is not None
            ):
                raise ValueError("endpoint baseline cannot load the Candidate Artifact")
        elif (
            self.activation_mode != "startup_overlay"
            or self.loaded_artifact_hash is None
            or self.import_attestation is None
        ):
            raise ValueError("endpoint Candidate requires startup Overlay attestation")
        return self


class EndpointRequestSample(StrictMeasurementModel):
    request_ordinal: int = Field(ge=0, le=1_000_000)
    succeeded: bool
    http_status: int | None = Field(default=None, ge=100, le=599)
    started_monotonic_ns: int = Field(ge=0)
    finished_monotonic_ns: int = Field(ge=0)
    e2e_latency_ns: int = Field(gt=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    finish_reason: str | None = Field(default=None, min_length=1, max_length=200)
    ttft_ns: int | None = Field(default=None, gt=0)
    tpot_ns: int | None = Field(default=None, ge=0)
    error_code: str | None = Field(default=None, min_length=1, max_length=200)
    raw_request: RawEvidenceFileV2
    raw_response: RawEvidenceFileV2

    @model_validator(mode="after")
    def require_complete_success_or_failure(self) -> EndpointRequestSample:
        if self.finished_monotonic_ns <= self.started_monotonic_ns:
            raise ValueError("endpoint request timing interval must be positive")
        if self.e2e_latency_ns != self.finished_monotonic_ns - self.started_monotonic_ns:
            raise ValueError("endpoint E2E latency must match the raw monotonic interval")
        success_fields = (self.prompt_tokens, self.completion_tokens, self.finish_reason)
        if self.succeeded:
            if self.http_status is None or not 200 <= self.http_status < 300:
                raise ValueError("successful endpoint request requires a 2xx response")
            if any(value is None for value in success_fields) or self.error_code is not None:
                raise ValueError("successful endpoint request has incomplete token evidence")
        elif self.error_code is None:
            raise ValueError("failed endpoint request requires a stable error code")
        if (self.ttft_ns is None) != (self.tpot_ns is None):
            raise ValueError("TTFT and TPOT must be present or absent together")
        return self


class EndpointLifecycleEvidence(StrictMeasurementModel):
    process_id: int = Field(ge=1)
    process_start_token: str = Field(min_length=1, max_length=200)
    started: ProcessLifecycleRecordV2
    started_raw: RawEvidenceFileV2
    reaped: ProcessLifecycleRecordV2
    reaped_raw: RawEvidenceFileV2
    ready_raw: RawEvidenceFileV2
    server_log: RawEvidenceFileV2

    @model_validator(mode="after")
    def bind_process(self) -> EndpointLifecycleEvidence:
        identity = (self.process_id, self.process_start_token)
        if (
            self.started.event != "started"
            or self.reaped.event != "reaped"
            or self.started.process_id != self.reaped.process_id
            or self.started.process_id != identity[0]
            or self.started.restart_ordinal != self.reaped.restart_ordinal
            or self.reaped.wait_status != 0
        ):
            raise ValueError("endpoint lifecycle does not prove one cleanly reaped process")
        return self


class EndpointCleanupEvidence(StrictMeasurementModel):
    process_reaped: Literal[True] = True
    container_absent: Literal[True] = True
    resource_restored: Literal[True] = True
    fence_raw: RawEvidenceFileV2
    health_raw: RawEvidenceFileV2
    cleanup_raw: RawEvidenceFileV2


class EndpointAcquisitionEvidence(StrictMeasurementModel):
    acquisition_ordinal: int = Field(ge=0, le=1_000_000)
    arm: EndpointArm
    lifecycle: EndpointLifecycleEvidence
    activation: EndpointActivationEvidence
    warmup_raw: tuple[RawEvidenceFileV2, ...] = Field(min_length=1)
    requests: tuple[EndpointRequestSample, ...] = Field(min_length=1)
    cleanup: EndpointCleanupEvidence
    producer_verdict: Literal[None] = None

    @field_validator("warmup_raw", "requests", mode="before")
    @classmethod
    def freeze_sequences(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_acquisition(self) -> EndpointAcquisitionEvidence:
        if self.activation.arm != self.arm:
            raise ValueError("endpoint activation belongs to another arm")
        if any(not sample.succeeded for sample in self.requests):
            raise ValueError("endpoint acquisition fails closed on any failed request")
        if tuple(item.request_ordinal for item in self.requests) != tuple(
            range(len(self.requests))
        ):
            raise ValueError("endpoint request ordinals must be contiguous")
        response_hashes = [item.raw_response.sha256 for item in self.requests]
        if len(response_hashes) != len(set(response_hashes)):
            raise ValueError("every endpoint request requires its own raw response")
        return self


class EndpointValidationBinding(StrictMeasurementModel):
    endpoint_run_id: UUID
    signed_m1: SignedM1EvidenceReference
    adapter_profile: str = Field(min_length=1, max_length=200)
    environment_fingerprint: str = Field(pattern=SHA256_PATTERN)
    lease: Stage0LeaseBinding

    @model_validator(mode="after")
    def require_exclusive_lease(self) -> EndpointValidationBinding:
        if self.lease.lease_scope is not LeaseScope.EXCLUSIVE:
            raise ValueError("endpoint validation requires an exclusive lease")
        return self


class EndpointValidationEvidence(StrictMeasurementModel):
    schema_version: Literal["m1-sglang-endpoint-evidence-v1"] = (
        "m1-sglang-endpoint-evidence-v1"
    )
    binding: EndpointValidationBinding
    workload: EndpointWorkloadSpec
    plan: EndpointMeasurementPlan
    plan_hash: str = Field(pattern=SHA256_PATTERN)
    acquisitions: tuple[EndpointAcquisitionEvidence, ...] = Field(min_length=4)
    adapter_provenance: tuple[Stage0AdapterProvenance, ...] = Field(min_length=1)
    evidence_index: RawEvidenceFileV2
    producer_verdict: Literal[None] = None
    automatic_release_allowed: Literal[False] = False

    @field_validator("acquisitions", "adapter_provenance", mode="before")
    @classmethod
    def freeze_sequences(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_evidence(self) -> EndpointValidationEvidence:
        if self.plan_hash != endpoint_plan_hash(self.plan):
            raise ValueError("endpoint plan hash differs from the canonical plan")
        if self.workload.target_id != self.binding.signed_m1.target_id:
            raise ValueError("endpoint workload target differs from the signed M1 target")
        if len(self.acquisitions) != len(self.plan.acquisition_order):
            raise ValueError("endpoint acquisitions do not match the frozen plan")
        identities: set[tuple[int, str]] = set()
        cache_namespaces: set[str] = set()
        for ordinal, (arm, acquisition) in enumerate(
            zip(self.plan.acquisition_order, self.acquisitions, strict=True)
        ):
            if acquisition.acquisition_ordinal != ordinal or acquisition.arm != arm:
                raise ValueError("endpoint acquisition order differs from the frozen plan")
            identity = (
                acquisition.lifecycle.process_id,
                acquisition.lifecycle.process_start_token,
            )
            if identity in identities:
                raise ValueError("every endpoint acquisition requires a fresh process")
            identities.add(identity)
            namespace = acquisition.activation.cache_namespace_hash
            if namespace in cache_namespaces:
                raise ValueError("every endpoint acquisition requires a fresh cache namespace")
            cache_namespaces.add(namespace)
            if acquisition.activation.image_digest != self.workload.image_digest:
                raise ValueError("endpoint acquisition used another image")
            if arm == "candidate" and (
                acquisition.activation.loaded_artifact_hash
                != self.binding.signed_m1.artifact_hash
            ):
                raise ValueError("endpoint Candidate loaded another Artifact")
            if len(acquisition.warmup_raw) != self.plan.warmup_requests:
                raise ValueError("endpoint warmup count differs from the frozen plan")
            if len(acquisition.requests) != self.plan.measured_requests_per_acquisition:
                raise ValueError("endpoint request count differs from the frozen plan")
            for sample in acquisition.requests:
                if (
                    sample.prompt_tokens != self.workload.expected_prompt_tokens
                    or sample.completion_tokens
                    != self.workload.expected_completion_tokens
                ):
                    raise ValueError("endpoint response token counts differ from the workload")
                if self.workload.stream != (sample.ttft_ns is not None):
                    raise ValueError("endpoint streaming metrics differ from the workload mode")
        if any(
            item.implementation_kind != "real"
            or item.capability != "endpoint_measurement_runner"
            or item.profile != self.binding.adapter_profile
            for item in self.adapter_provenance
        ):
            raise ValueError("endpoint evidence requires the registered real Runner provenance")
        return self


def endpoint_plan_hash(plan: EndpointMeasurementPlan) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(plan)).hexdigest()


__all__ = [
    "EndpointAcquisitionEvidence",
    "EndpointActivationEvidence",
    "EndpointCleanupEvidence",
    "EndpointLifecycleEvidence",
    "EndpointMeasurementPlan",
    "EndpointRequestSample",
    "EndpointValidationBinding",
    "EndpointValidationEvidence",
    "EndpointWorkloadSpec",
    "SignedM1EvidenceReference",
    "endpoint_plan_hash",
]
