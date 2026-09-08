# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""M1 failure evidence and explicit attempted-versus-verified accounting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import Field, model_validator

from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.measurement.evidence import EvidenceArtifact
from hcuopt.measurement.harness import MeasurementSafetyError
from hcuopt.measurement.m1_models import M1AcquisitionEvidence, M1MeasurementBinding, M1RawSample
from hcuopt.measurement.models import ClockCalibrationV2, StrictMeasurementModel


@dataclass
class M1SamplingProgress:
    attempted_sample_count: int = 0
    verified_samples: list[M1RawSample] = field(default_factory=list)


class M1FailureReport(StrictMeasurementModel):
    schema_version: Literal["m1-measurement-failure-v2"] = "m1-measurement-failure-v2"
    binding: M1MeasurementBinding
    plan_hash: str = Field(pattern=SHA256_PATTERN)
    formal_execution_request_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    expected_sample_count: int = Field(ge=1)
    attempted_sample_count: int = Field(ge=0)
    verified_samples: tuple[M1RawSample, ...]
    completed_acquisitions: tuple[M1AcquisitionEvidence, ...]
    calibration: ClockCalibrationV2 | None
    cleanup_evidence: dict[str, Any]
    error_type: str
    error: str
    accounting_basis: Literal["invoked_sample_batches_including_failed_call"] = (
        "invoked_sample_batches_including_failed_call"
    )
    producer_verdict: None = None
    synthetic: Literal[False] = False

    @model_validator(mode="after")
    def validate_progress(self) -> M1FailureReport:
        if (
            not len(self.verified_samples)
            <= self.attempted_sample_count
            <= self.expected_sample_count
        ):
            raise ValueError("failure sampling counters are inconsistent")
        completed = tuple(sample for item in self.completed_acquisitions for sample in item.samples)
        if completed != self.verified_samples[: len(completed)]:
            raise ValueError("completed acquisitions differ from verified failure samples")
        identities = {
            (item.acquisition_ordinal, item.sample_ordinal) for item in self.verified_samples
        }
        if len(identities) != len(self.verified_samples):
            raise ValueError("failure sample identity was reused")
        return self


class M1MeasurementFailure(MeasurementSafetyError):
    """Carries a published file reference, not an unverified in-memory usage count."""

    def __init__(self, message: str, evidence: EvidenceArtifact | None) -> None:
        super().__init__(message)
        self.evidence = evidence
