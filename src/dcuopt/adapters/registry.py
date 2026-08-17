from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dcuopt.adapters.interfaces import (
    ArtifactStoreAdapter,
    BuilderAdapter,
    CandidateGenerator,
    EvaluatorAdapter,
    ExecutionAdapter,
    MeasurementHarness,
    ProfilerAdapter,
    ResourceCleaner,
    SourceManagerAdapter,
)
from dcuopt.contracts.platform_v1 import AdapterProvenance
from dcuopt.domain.errors import AdapterUnavailable


@dataclass(frozen=True, slots=True)
class AdapterRegistry:
    """One explicit set of replaceable boundaries used by a worker profile."""

    profile: str
    profiler: ProfilerAdapter | None = None
    candidate_generator: CandidateGenerator | None = None
    builder: BuilderAdapter | None = None
    measurement_harness: MeasurementHarness | None = None
    evaluator: EvaluatorAdapter | None = None
    resource_cleaner: ResourceCleaner | None = None
    executor: ExecutionAdapter | None = None
    source_manager: SourceManagerAdapter | None = None
    artifact_store: ArtifactStoreAdapter | None = None

    @classmethod
    def fake(cls) -> AdapterRegistry:
        from dcuopt.adapters.fake import (
            FakeArtifactStore,
            FakeBuilder,
            FakeCandidateGenerator,
            FakeEvaluator,
            FakeExecutionAdapter,
            FakeMeasurementHarness,
            FakeProfiler,
            FakeResourceCleaner,
            FakeSourceManager,
        )

        return cls(
            profile="fake-v1-control-flow-only",
            profiler=FakeProfiler(),
            candidate_generator=FakeCandidateGenerator(),
            builder=FakeBuilder(),
            measurement_harness=FakeMeasurementHarness(),
            evaluator=FakeEvaluator(),
            resource_cleaner=FakeResourceCleaner(),
            executor=FakeExecutionAdapter(),
            source_manager=FakeSourceManager(),
            artifact_store=FakeArtifactStore(),
        )

    def available(self) -> tuple[str, ...]:
        names = (
            "profiler",
            "candidate_generator",
            "builder",
            "measurement_harness",
            "evaluator",
            "resource_cleaner",
            "executor",
            "source_manager",
            "artifact_store",
        )
        return tuple(name for name in names if getattr(self, name) is not None)

    def require(self, name: str) -> Any:
        if name not in self.__dataclass_fields__ or name == "profile":
            raise AdapterUnavailable(f"unknown adapter boundary: {name}")
        adapter = getattr(self, name)
        if adapter is None:
            raise AdapterUnavailable(f"adapter {name} is unavailable in profile {self.profile}")
        provenance = getattr(adapter, "provenance", None)
        if not isinstance(provenance, AdapterProvenance):
            raise AdapterUnavailable(
                f"adapter {name} in profile {self.profile} has no valid provenance"
            )
        if provenance.profile != self.profile:
            raise AdapterUnavailable(
                f"adapter {name} provenance profile {provenance.profile} "
                f"does not match registry profile {self.profile}"
            )
        return adapter
