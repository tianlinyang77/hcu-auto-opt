from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from dcuopt.domain.models import OptimizationCandidate


class ProfilerAdapter(Protocol):
    def probe(self) -> Mapping[str, Any]: ...

    def profile(self, workload_id: str, output_dir: Path) -> Sequence[Mapping[str, Any]]: ...


class CandidateGenerator(Protocol):
    def generate(self, hotspot: Mapping[str, Any]) -> Sequence[OptimizationCandidate]: ...


class MeasurementHarness(Protocol):
    def run(self, plan: Mapping[str, Any], output_dir: Path) -> Mapping[str, Any]: ...


class ResourceCleaner(Protocol):
    def fence(self, resource_id: str, fencing_token: int) -> Mapping[str, Any]: ...

    def health_check(self, resource_id: str) -> Mapping[str, Any]: ...

