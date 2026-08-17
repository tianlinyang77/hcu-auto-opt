from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from dcuopt.adapters.registry import AdapterRegistry


class JobHandlers:
    def __init__(self, adapters: AdapterRegistry, output_dir: Path | None = None) -> None:
        self.adapters = adapters
        self.output_dir = output_dir or Path(".")

    def handle(self, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"handle_{job_type}", None)
        if handler is None:
            raise ValueError(f"unsupported job type: {job_type}")
        return handler(payload)

    def handle_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        profiler = self.adapters.require("profiler")
        return dict(profiler.profile(payload["workload_id"], self.output_dir)[0])

    def handle_candidate_generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        generator = self.adapters.require("candidate_generator")
        candidates = []
        for item in generator.generate(payload["hotspot"]):
            candidate = dict(item)
            candidate["candidate_id"] = str(
                uuid5(NAMESPACE_URL, f"dcuopt:{payload['task_id']}:{candidate['ordinal']}")
            )
            candidates.append(candidate)
        round_id = uuid5(NAMESPACE_URL, f"dcuopt:{payload['task_id']}:round:0")
        return {"round_id": str(round_id), "candidates": candidates, "synthetic": True}

    def handle_build(self, payload: dict[str, Any]) -> dict[str, Any]:
        builder = self.adapters.require("builder")
        artifact = builder.build(payload, self.output_dir)
        return artifact.model_dump(mode="json")

    def handle_correctness(self, payload: dict[str, Any]) -> dict[str, Any]:
        evaluator = self.adapters.require("evaluator")
        return dict(evaluator.correctness(payload, self.output_dir))

    def handle_performance(self, payload: dict[str, Any]) -> dict[str, Any]:
        harness = self.adapters.require("measurement_harness")
        return dict(harness.run({"phase": "performance", **payload}, self.output_dir))

    def handle_e2e(self, payload: dict[str, Any]) -> dict[str, Any]:
        evaluator = self.adapters.require("evaluator")
        return dict(evaluator.e2e(payload, self.output_dir))


class FakeJobHandlers(JobHandlers):
    def __init__(self, output_dir: Path | None = None) -> None:
        super().__init__(AdapterRegistry.fake(), output_dir)


def candidate_id(payload: dict[str, Any]) -> UUID:
    return UUID(payload["candidate_id"])
