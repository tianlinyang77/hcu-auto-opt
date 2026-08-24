from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from hcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    ArtifactManifest,
    ExecutionRequest,
    ExecutionResult,
    MeasurementSeries,
    SourceSnapshot,
    TargetSpec,
)
from hcuopt.domain.models import OptimizationCandidate
from hcuopt.measurement.stage0 import Stage0ProbeOutput


class ProfilerAdapter(Protocol):
    provenance: AdapterProvenance

    def probe(self) -> Mapping[str, Any]: ...

    def profile(self, workload_id: str, output_dir: Path) -> Sequence[Mapping[str, Any]]: ...


class CandidateGenerator(Protocol):
    provenance: AdapterProvenance

    def generate(
        self, hotspot: Mapping[str, Any]
    ) -> Sequence[OptimizationCandidate | Mapping[str, Any]]: ...


class BuilderAdapter(Protocol):
    provenance: AdapterProvenance

    def build(self, candidate: Mapping[str, Any], output_dir: Path) -> ArtifactManifest: ...


class MeasurementHarness(Protocol):
    provenance: AdapterProvenance

    def run(self, plan: Mapping[str, Any], output_dir: Path) -> MeasurementSeries: ...


@runtime_checkable
class ManualPerformanceMeasurementHarness(Protocol):
    provenance: AdapterProvenance

    def run_manual_performance(
        self, payload: Mapping[str, Any], output_dir: Path
    ) -> Any: ...


class Stage0ProbeAdapter(Protocol):
    provenance: AdapterProvenance
    target_fingerprint: str

    def run_probe(self, payload: Mapping[str, Any], output_dir: Path) -> Stage0ProbeOutput: ...


class EvaluatorAdapter(Protocol):
    provenance: AdapterProvenance

    def correctness(self, plan: Mapping[str, Any], output_dir: Path) -> Mapping[str, Any]: ...

    def e2e(self, plan: Mapping[str, Any], output_dir: Path) -> Mapping[str, Any]: ...


@runtime_checkable
class PairedFrameworkSmokeEvaluator(Protocol):
    provenance: AdapterProvenance

    def prepare_framework_smoke(
        self,
        *,
        target: TargetSpec,
        artifact: ArtifactManifest,
        output_dir: Path,
        task_id: UUID,
        evaluation_run_id: UUID,
        attempt_number: int,
        baseline_request_id: UUID,
        noop_request_id: UUID,
        resource_id: str,
        fencing_token: int,
    ) -> Any: ...

    def evaluate_framework_smoke(self, plan: Any, **values: Any) -> Mapping[str, Any]: ...


class ResourceCleaner(Protocol):
    provenance: AdapterProvenance

    def fence(self, resource_id: str, fencing_token: int) -> Mapping[str, Any]: ...

    def health_check(self, resource_id: str) -> Mapping[str, Any]: ...


class ExecutionAdapter(Protocol):
    provenance: AdapterProvenance

    def execute(
        self,
        request: ExecutionRequest,
        target: TargetSpec,
        output_dir: Path,
    ) -> ExecutionResult: ...

    def cancel(self, request_id: UUID) -> Mapping[str, Any]: ...


class SourceManagerAdapter(Protocol):
    provenance: AdapterProvenance

    def prepare_baseline(self, target: TargetSpec, output_dir: Path) -> SourceSnapshot: ...

    def create_candidate(
        self,
        baseline: SourceSnapshot,
        candidate_id: UUID,
        output_dir: Path,
    ) -> SourceSnapshot: ...

    def remove_candidate(
        self,
        baseline: SourceSnapshot,
        candidate: SourceSnapshot,
        output_dir: Path,
    ) -> None: ...

    def recover_candidates(
        self,
        baseline: SourceSnapshot,
        output_dir: Path,
    ) -> Sequence[str]: ...


class ArtifactStoreAdapter(Protocol):
    provenance: AdapterProvenance

    def publish(self, manifest: ArtifactManifest, source_path: Path) -> ArtifactManifest: ...


class JobHandler(Protocol):
    def handle(self, job_type: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    def cleanup(self, job_type: str, payload: dict[str, Any]) -> dict[str, Any]: ...
