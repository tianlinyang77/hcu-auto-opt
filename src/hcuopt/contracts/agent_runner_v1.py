# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import json
from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import SHA256_PATTERN

RUNNER_EXECUTION_CONTRACT_VERSION = "m2b-runner-execution-v1"

RunnerExecutionStatus = Literal[
    "succeeded",
    "failed",
    "timed_out",
    "output_limit_exceeded",
    "token_limit_exceeded",
    "invalid_output",
    "cleanup_failed",
]


def _canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n").encode("utf-8")


def runner_provenance_identity_hash(
    *,
    profile: str,
    capability: str,
    adapter_name: str,
    adapter_version: str,
    implementation_kind: str,
    source_commit: str | None,
) -> str:
    """Hash the complete stable identity of a Runner adapter."""

    payload = {
        "adapter_name": adapter_name,
        "adapter_version": adapter_version,
        "capability": capability,
        "implementation_kind": implementation_kind,
        "profile": profile,
        "source_commit": source_commit,
    }
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


class RunnerProvenance(ContractModel):
    profile: str = Field(min_length=1, max_length=200)
    capability: Literal["agent_runner"] = "agent_runner"
    adapter_name: str = Field(min_length=1, max_length=200)
    adapter_version: str = Field(min_length=1, max_length=200)
    implementation_kind: Literal["real", "fake"]
    source_commit: str | None = Field(default=None, min_length=1, max_length=200)
    identity_hash: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def require_reproducible_identity(self) -> RunnerProvenance:
        expected = runner_provenance_identity_hash(
            profile=self.profile,
            capability=self.capability,
            adapter_name=self.adapter_name,
            adapter_version=self.adapter_version,
            implementation_kind=self.implementation_kind,
            source_commit=self.source_commit,
        )
        if self.identity_hash != expected:
            raise ValueError("Runner Provenance identity Hash does not match its fields")
        return self


class RunnerExecutionRecord(ContractModel):
    """One bounded Runner execution before deployment-owned publication."""

    schema_version: Literal["m2b-runner-execution-record-v1"] = (
        "m2b-runner-execution-record-v1"
    )
    attempt_id: UUID
    generation_run_id: UUID
    request_id: UUID
    request_hash: str = Field(pattern=SHA256_PATTERN)
    plan_id: UUID
    generator_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    attempt_number: int = Field(ge=1, le=8)
    runner_provenance: RunnerProvenance
    generator_artifact_hash: str = Field(pattern=SHA256_PATTERN)
    executable_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    status: RunnerExecutionStatus
    synthetic: bool
    attempts_consumed: Literal[1] = 1
    wall_seconds_consumed: float = Field(ge=0)
    stdout_bytes_consumed: int = Field(ge=0)
    stderr_bytes_consumed: int = Field(ge=0)
    total_output_bytes_consumed: int = Field(ge=0)
    tokens_consumed: int | None = Field(default=None, ge=0)
    exit_code: int | None = None
    executable_name: str = Field(min_length=1, max_length=300)
    argv_hash: str = Field(pattern=SHA256_PATTERN)
    input_manifest_hash: str = Field(pattern=SHA256_PATTERN)
    stdout_hash: str = Field(pattern=SHA256_PATTERN)
    stderr_hash: str = Field(pattern=SHA256_PATTERN)
    stdout_summary: str = Field(max_length=4096)
    stderr_summary: str = Field(max_length=4096)
    environment_names: tuple[str, ...] = ()
    termination_reason: str | None = Field(default=None, min_length=1, max_length=200)
    process_tree_cleanup: Literal["not_needed", "terminated", "killed", "failed"]
    cleanup_status: Literal["verified", "failed"]
    cleanup_summary: str = Field(min_length=1, max_length=1000)
    performance_conclusion: Literal["not_measured"] = "not_measured"
    hcu_access_allowed: Literal[False] = False
    holdout_access_allowed: Literal[False] = False
    measurement_access_allowed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_coherent_execution(self) -> RunnerExecutionRecord:
        if self.total_output_bytes_consumed != (
            self.stdout_bytes_consumed + self.stderr_bytes_consumed
        ):
            raise ValueError("Runner total output bytes differ from its streams")
        if self.cleanup_status == "verified" and self.process_tree_cleanup == "failed":
            raise ValueError("Runner cleanup cannot be verified after process cleanup failed")
        if self.cleanup_status == "failed" and self.status != "cleanup_failed":
            raise ValueError("failed Runner cleanup requires cleanup_failed status")
        if self.status == "succeeded":
            if self.exit_code != 0:
                raise ValueError("successful Runner execution requires exit_code 0")
            if self.cleanup_status != "verified" or self.tokens_consumed is None:
                raise ValueError("successful Runner execution requires usage and verified cleanup")
        if tuple(sorted(self.environment_names)) != self.environment_names:
            raise ValueError("Runner environment names must be sorted")
        if len(set(self.environment_names)) != len(self.environment_names):
            raise ValueError("Runner environment names must be unique")
        return self


def runner_execution_receipt_id_for(attempt_id: UUID) -> UUID:
    return uuid5(attempt_id, "hcuopt:m2b-runner-execution-receipt:v1")


class RunnerExecutionReceipt(ContractModel):
    """Immutable deployment receipt for one public Runner execution record."""

    schema_version: Literal["m2b-runner-execution-receipt-v1"] = (
        "m2b-runner-execution-receipt-v1"
    )
    receipt_id: UUID
    execution: RunnerExecutionRecord
    raw_output_uri: str | None = Field(default=None, min_length=1, max_length=4000)
    raw_output_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    raw_output_bytes: int = Field(ge=0)
    dev_only: Literal[True] = True
    formal_intake_allowed: Literal[False] = False
    hcu_access_allowed: Literal[False] = False
    measurement_access_allowed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_receipt_binding(self) -> RunnerExecutionReceipt:
        if self.receipt_id != runner_execution_receipt_id_for(self.execution.attempt_id):
            raise ValueError("Runner Receipt identity differs from its Attempt")
        has_raw_output = self.raw_output_uri is not None and self.raw_output_hash is not None
        if (self.raw_output_uri is None) != (self.raw_output_hash is None):
            raise ValueError("Runner raw output reference must be atomic")
        if self.execution.status == "succeeded":
            if (
                not has_raw_output
                or self.raw_output_hash != self.execution.stdout_hash
                or self.raw_output_bytes != self.execution.stdout_bytes_consumed
            ):
                raise ValueError("successful Runner Receipt must bind exact stdout bytes")
        elif has_raw_output or self.raw_output_bytes != 0:
            raise ValueError("unsuccessful Runner Receipt cannot expose proposal output")
        return self


def runner_execution_receipt_hash(receipt: RunnerExecutionReceipt) -> str:
    payload = _canonical_json_bytes(receipt.model_dump(mode="json"))
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class RunnerExecutionReceiptRef(ContractModel):
    schema_version: Literal["m2b-runner-execution-receipt-ref-v1"] = (
        "m2b-runner-execution-receipt-ref-v1"
    )
    receipt_id: UUID
    attempt_id: UUID
    generation_run_id: UUID
    request_id: UUID
    request_hash: str = Field(pattern=SHA256_PATTERN)
    uri: str = Field(min_length=1, max_length=4000)
    content_hash: str = Field(pattern=SHA256_PATTERN)


__all__ = [
    "RUNNER_EXECUTION_CONTRACT_VERSION",
    "RunnerExecutionReceipt",
    "RunnerExecutionReceiptRef",
    "RunnerExecutionRecord",
    "RunnerExecutionStatus",
    "RunnerProvenance",
    "runner_provenance_identity_hash",
    "runner_execution_receipt_hash",
    "runner_execution_receipt_id_for",
]
