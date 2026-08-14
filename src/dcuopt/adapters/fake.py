from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


class FakeProfiler:
    def probe(self) -> Mapping[str, Any]:
        return {
            "capability": "full",
            "tool": "fake-profiler-v1",
            "warning": "control-flow fixture; not a real DCU probe",
        }

    def profile(self, workload_id: str, output_dir: Path) -> Sequence[Mapping[str, Any]]:
        return [
            {
                "symbol": "fixture_rmsnorm",
                "share_ratio": 0.18,
                "opportunity_score": 0.72,
                "patchability": "hot_patch",
                "workload_id": workload_id,
                "evidence_uri": f"fake://profile/{workload_id}",
            }
        ]


class FakeCandidateGenerator:
    def generate(self, hotspot: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
        return [
            {
                "ordinal": 0,
                "variant": "fixture-invalid-fast-path",
                "source_hash": self._hash(f"{hotspot['symbol']}:invalid"),
            },
            {
                "ordinal": 1,
                "variant": "fixture-safe-vectorized-path",
                "source_hash": self._hash(f"{hotspot['symbol']}:safe"),
            },
        ]

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()


class FakeBuilder:
    def build(self, candidate: Mapping[str, Any], output_dir: Path) -> Mapping[str, Any]:
        content_hash = hashlib.sha256(
            f"artifact:{candidate['source_hash']}".encode()
        ).hexdigest()
        return {
            "kind": "fake_shared_object",
            "uri": f"fake://artifacts/{content_hash}.so",
            "content_hash": content_hash,
            "metadata": {
                "builder": "fake-builder-v1",
                "sbom": "fake://sbom/control-flow-only",
                "signed": False,
            },
        }


class FakeMeasurementHarness:
    """The only fake timing entry point; it never claims real performance."""

    def run(self, plan: Mapping[str, Any], output_dir: Path) -> Mapping[str, Any]:
        phase = plan.get("phase")
        if phase == "performance":
            return {
                "passed": True,
                "speedup_ratio": 1.08,
                "ci_low": 1.06,
                "ci_high": 1.10,
                "measurement_protocol": "fake-v1-control-flow-only",
                "synthetic": True,
            }
        if phase == "e2e":
            return {
                "passed": True,
                "e2e_speedup_ratio": 1.04,
                "abba_order": ["A", "B", "B", "A"],
                "measurement_protocol": "fake-v1-control-flow-only",
                "synthetic": True,
            }
        raise ValueError(f"unsupported fake measurement phase: {phase}")


class FakeEvaluator:
    def correctness(self, plan: Mapping[str, Any], output_dir: Path) -> Mapping[str, Any]:
        passed = plan["variant"] != "fixture-invalid-fast-path"
        return {
            "passed": passed,
            "max_abs_error": 0.0 if passed else 0.25,
            "reason": "fixture pass" if passed else "fixture intentionally rejected",
            "synthetic": True,
        }

    def e2e(self, plan: Mapping[str, Any], output_dir: Path) -> Mapping[str, Any]:
        return FakeMeasurementHarness().run({"phase": "e2e", **plan}, output_dir)


class FakeResourceCleaner:
    def fence(self, resource_id: str, fencing_token: int) -> Mapping[str, Any]:
        return {
            "resource_id": resource_id,
            "fencing_token": fencing_token,
            "processes_terminated": True,
            "clocks_restored": True,
            "memory_released": True,
            "synthetic": True,
        }

    def health_check(self, resource_id: str) -> Mapping[str, Any]:
        return {"resource_id": resource_id, "healthy": True, "synthetic": True}
