from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from dcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    ArtifactManifest,
    ExecutionRequest,
    ExecutionResult,
    MeasurementSeries,
    SourceSnapshot,
    TargetSpec,
)
from dcuopt.domain.models import OptimizationCandidate


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


class EvaluatorAdapter(Protocol):
    provenance: AdapterProvenance

    def correctness(self, plan: Mapping[str, Any], output_dir: Path) -> Mapping[str, Any]: ...

    def e2e(self, plan: Mapping[str, Any], output_dir: Path) -> Mapping[str, Any]: ...


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


class SourceManagerAdapter(Protocol):
    provenance: AdapterProvenance

    def prepare_baseline(self, target: TargetSpec, output_dir: Path) -> SourceSnapshot: ...

    def create_candidate(
        self,
        baseline: SourceSnapshot,
        candidate_id: UUID,
        output_dir: Path,
    ) -> SourceSnapshot: ...


class ArtifactStoreAdapter(Protocol):
    provenance: AdapterProvenance

    def publish(self, manifest: ArtifactManifest, source_path: Path) -> ArtifactManifest: ...


class JobHandler(Protocol):
    def handle(self, job_type: str, payload: dict[str, Any]) -> dict[str, Any]: ...
