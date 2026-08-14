from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from dcuopt.adapters.fake import (
    FakeBuilder,
    FakeCandidateGenerator,
    FakeEvaluator,
    FakeMeasurementHarness,
    FakeProfiler,
)


class FakeJobHandlers:
    def __init__(self) -> None:
        self.profiler = FakeProfiler()
        self.generator = FakeCandidateGenerator()
        self.builder = FakeBuilder()
        self.harness = FakeMeasurementHarness()
        self.evaluator = FakeEvaluator()
        self.output_dir = Path(".")

    def handle(self, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"handle_{job_type}", None)
        if handler is None:
            raise ValueError(f"unsupported job type: {job_type}")
        return handler(payload)

    def handle_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.profiler.profile(payload["workload_id"], self.output_dir)[0])

    def handle_candidate_generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        candidates = []
        for item in self.generator.generate(payload["hotspot"]):
            candidate = dict(item)
            candidate["candidate_id"] = str(
                uuid5(NAMESPACE_URL, f"dcuopt:{payload['task_id']}:{candidate['ordinal']}")
            )
            candidates.append(candidate)
        round_id = uuid5(NAMESPACE_URL, f"dcuopt:{payload['task_id']}:round:0")
        return {"round_id": str(round_id), "candidates": candidates, "synthetic": True}

    def handle_build(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.builder.build(payload, self.output_dir))

    def handle_correctness(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.evaluator.correctness(payload, self.output_dir))

    def handle_performance(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.harness.run({"phase": "performance", **payload}, self.output_dir))

    def handle_e2e(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.evaluator.e2e(payload, self.output_dir))


def candidate_id(payload: dict[str, Any]) -> UUID:
    return UUID(payload["candidate_id"])
