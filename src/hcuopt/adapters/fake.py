from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
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
from hcuopt.domain.enums import Stage0ProbeType
from hcuopt.measurement.stage0 import Stage0ProbeOutput


def _fake_provenance(capability: str, adapter_name: str) -> AdapterProvenance:
    return AdapterProvenance(
        profile="fake-v1-control-flow-only",
        capability=capability,
        adapter_name=adapter_name,
        adapter_version="1",
        implementation_kind="fake",
    )


class FakeProfiler:
    provenance = _fake_provenance("profiler", "FakeProfiler")

    def probe(self) -> Mapping[str, Any]:
        return {
            "capability": "full",
            "tool": "fake-profiler-v1",
            "warning": "control-flow fixture; not a real HCU probe",
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


class FakeStage0Probe:
    """Synthetic seven-probe implementation for Dry Run control-flow tests."""

    provenance = _fake_provenance("stage0_probe", "FakeStage0Probe")

    _summaries: dict[Stage0ProbeType, dict[str, Any]] = {
        Stage0ProbeType.FINGERPRINT: {
            "hardware_fingerprint": "synthetic-hardware",
            "software_fingerprint": "synthetic-software",
        },
        Stage0ProbeType.TIMER: {"timer_resolution_ns": 0.0},
        Stage0ProbeType.NOISE: {
            "gate_result": "fail",
            "noise_sigma_ns": 0.0,
            "noise_cv": 0.0,
            "mde_ratio": 0.0,
        },
        Stage0ProbeType.KNOWN_SIGNAL: {"detected": False},
        Stage0ProbeType.NULL_SIGNAL: {"false_positive": False},
        Stage0ProbeType.PROFILER: {"capability": "none"},
        Stage0ProbeType.HOTPATCH: {"capability": "none"},
    }

    def run_probe(
        self, payload: Mapping[str, Any], output_dir: Path
    ) -> Stage0ProbeOutput:
        del output_dir
        probe_type = Stage0ProbeType(str(payload["probe_type"]))
        return Stage0ProbeOutput(
            summary=self._summaries[probe_type],
            raw_evidence_uri=f"fake://stage0/{probe_type.value}",
            raw_evidence_hash="sha256:" + "0" * 64,
            cleanup_evidence=None,
            synthetic=True,
        )


class FakeCandidateGenerator:
    provenance = _fake_provenance("candidate_generator", "FakeCandidateGenerator")

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
    provenance = _fake_provenance("builder", "FakeBuilder")

    def build(self, candidate: Mapping[str, Any], output_dir: Path) -> ArtifactManifest:
        digest = hashlib.sha256(
            f"artifact:{candidate['source_hash']}".encode()
        ).hexdigest()
        candidate_id = candidate.get("candidate_id")
        return ArtifactManifest(
            candidate_id=UUID(str(candidate_id)) if candidate_id is not None else None,
            kind="fake_shared_object",
            uri=f"fake://artifacts/{digest}.so",
            content_hash=f"sha256:{digest}",
            metadata={
                "builder": "fake-builder-v1",
                "sbom": "fake://sbom/control-flow-only",
                "signed": False,
            },
            sbom_uri="fake://sbom/control-flow-only",
            synthetic=True,
        )


class FakeMeasurementHarness:
    """The only fake timing entry point; it never claims real performance."""

    provenance = _fake_provenance("measurement_harness", "FakeMeasurementHarness")

    def run(self, plan: Mapping[str, Any], output_dir: Path) -> MeasurementSeries:
        phase = plan.get("phase")
        if phase in {"performance", "e2e"}:
            return MeasurementSeries(
                status="not_measured",
                metric_name="latency",
                unit="ns",
                protocol_version="fake-v1-control-flow-only",
                summary={
                    "reason": "control-flow fixture; no timing samples were collected",
                    "requested_phase": phase,
                },
                adapter_provenance=self.provenance,
                synthetic=True,
            )
        raise ValueError(f"unsupported fake measurement phase: {phase}")


class FakeEvaluator:
    provenance = _fake_provenance("evaluator", "FakeEvaluator")

    def correctness(self, plan: Mapping[str, Any], output_dir: Path) -> Mapping[str, Any]:
        passed = plan["variant"] != "fixture-invalid-fast-path"
        return {
            "passed": passed,
            "max_abs_error": 0.0 if passed else 0.25,
            "reason": "fixture pass" if passed else "fixture intentionally rejected",
            "synthetic": True,
        }

    def e2e(self, plan: Mapping[str, Any], output_dir: Path) -> Mapping[str, Any]:
        return {
            "passed": True,
            "reason": "control-flow fixture; E2E performance was not measured",
            "synthetic": True,
        }


class FakeResourceCleaner:
    provenance = _fake_provenance("resource_cleaner", "FakeResourceCleaner")

    def fence(self, resource_id: str, fencing_token: int) -> Mapping[str, Any]:
        return {
            "resource_id": resource_id,
            "fencing_token": fencing_token,
            "processes_terminated": True,
            "clocks_restored": True,
            "memory_released": True,
            "fenced": True,
            "synthetic": True,
        }

    def health_check(self, resource_id: str) -> Mapping[str, Any]:
        return {"resource_id": resource_id, "healthy": True, "synthetic": True}


class FakeExecutionAdapter:
    provenance = _fake_provenance("executor", "FakeExecutionAdapter")

    def execute(
        self,
        request: ExecutionRequest,
        target: TargetSpec,
        output_dir: Path,
    ) -> ExecutionResult:
        if request.target_id != target.target_id:
            raise ValueError(
                f"execution target mismatch: request={request.target_id}, target={target.target_id}"
            )
        now = datetime.now(timezone.utc)
        return ExecutionResult(
            request_id=request.request_id,
            status="succeeded",
            exit_code=0,
            started_at=now,
            finished_at=now,
            stdout_uri=f"fake://execution/{request.request_id}/stdout",
            stderr_uri=f"fake://execution/{request.request_id}/stderr",
            metadata={"target_id": target.target_id, "warning": "control-flow only"},
            adapter_provenance=self.provenance,
            synthetic=True,
        )

    def cancel(self, request_id: UUID) -> Mapping[str, Any]:
        return {
            "request_id": str(request_id),
            "cancel_requested": True,
            "owned_execution_found": False,
            "synthetic": True,
        }


class FakeSourceManager:
    provenance = _fake_provenance("source_manager", "FakeSourceManager")

    def prepare_baseline(self, target: TargetSpec, output_dir: Path) -> SourceSnapshot:
        digest = hashlib.sha256(target.source_baseline.commit.encode()).hexdigest()
        return SourceSnapshot(
            kind="baseline",
            repository=target.source_baseline.repository,
            commit=target.source_baseline.commit,
            tree_hash=target.source_baseline.commit,
            source_hash=f"sha256:{digest}",
            worktree_uri=f"fake://source/{target.target_id}/baseline",
            clean=True,
        )

    def create_candidate(
        self,
        baseline: SourceSnapshot,
        candidate_id: UUID,
        output_dir: Path,
    ) -> SourceSnapshot:
        digest = hashlib.sha256(f"{baseline.source_hash}:{candidate_id}".encode()).hexdigest()
        return SourceSnapshot(
            kind="candidate",
            repository=baseline.repository,
            commit=baseline.commit,
            tree_hash=baseline.tree_hash,
            source_hash=f"sha256:{digest}",
            worktree_uri=f"fake://source/candidates/{candidate_id}",
            clean=True,
            parent_snapshot_id=baseline.snapshot_id,
        )

    def remove_candidate(
        self,
        baseline: SourceSnapshot,
        candidate: SourceSnapshot,
        output_dir: Path,
    ) -> None:
        if candidate.parent_snapshot_id != baseline.snapshot_id:
            raise ValueError("candidate does not belong to the supplied baseline")

    def recover_candidates(
        self,
        baseline: SourceSnapshot,
        output_dir: Path,
    ) -> tuple[str, ...]:
        return ()


class FakeArtifactStore:
    provenance = _fake_provenance("artifact_store", "FakeArtifactStore")

    def publish(self, manifest: ArtifactManifest, source_path: Path) -> ArtifactManifest:
        if not manifest.synthetic:
            raise ValueError("fake artifact store only accepts synthetic manifests")
        return manifest
