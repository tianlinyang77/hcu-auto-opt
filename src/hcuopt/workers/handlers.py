from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.contracts.platform_v1 import (
    ArtifactManifest,
    EvaluationRun,
    EvidenceBundle,
    ExecutionAttempt,
    ExecutionRequest,
    SourceSnapshot,
    TargetSpec,
)
from hcuopt.contracts.v1 import (
    FrameworkSmokeResult,
    NoopBuildResult,
    SourcePreparationResult,
)
from hcuopt.domain.enums import LeaseScope


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
        result = dict(profiler.profile(payload["workload_id"], self.output_dir)[0])
        result["adapter_provenance"] = [profiler.provenance.model_dump(mode="json")]
        return result

    def handle_candidate_generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        generator = self.adapters.require("candidate_generator")
        candidates = []
        for item in generator.generate(payload["hotspot"]):
            candidate = dict(item)
            candidate["candidate_id"] = str(
                uuid5(NAMESPACE_URL, f"hcuopt:{payload['task_id']}:{candidate['ordinal']}")
            )
            candidates.append(candidate)
        round_id = uuid5(NAMESPACE_URL, f"hcuopt:{payload['task_id']}:round:0")
        return {
            "round_id": str(round_id),
            "candidates": candidates,
            "adapter_provenance": [generator.provenance.model_dump(mode="json")],
            "synthetic": True,
        }

    def handle_build(self, payload: dict[str, Any]) -> dict[str, Any]:
        builder = self.adapters.require("builder")
        artifact = builder.build(payload, self.output_dir)
        result = artifact.model_dump(mode="json")
        result["metadata"]["adapter_provenance"] = [
            builder.provenance.model_dump(mode="json")
        ]
        return result

    def handle_correctness(self, payload: dict[str, Any]) -> dict[str, Any]:
        evaluator = self.adapters.require("evaluator")
        result = dict(evaluator.correctness(payload, self.output_dir))
        result["adapter_provenance"] = [evaluator.provenance.model_dump(mode="json")]
        return result

    def handle_performance(self, payload: dict[str, Any]) -> dict[str, Any]:
        harness = self.adapters.require("measurement_harness")
        measurement = harness.run({"phase": "performance", **payload}, self.output_dir)
        return {
            "passed": True,
            "measurement": measurement.model_dump(mode="json"),
            "adapter_provenance": [harness.provenance.model_dump(mode="json")],
            "synthetic": measurement.synthetic,
        }

    def handle_e2e(self, payload: dict[str, Any]) -> dict[str, Any]:
        evaluator = self.adapters.require("evaluator")
        result = dict(evaluator.e2e(payload, self.output_dir))
        result["adapter_provenance"] = [evaluator.provenance.model_dump(mode="json")]
        return result

    def handle_source_prepare(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = TargetSpec.model_validate(payload["target"])
        manager = self.adapters.require("source_manager")
        source = manager.prepare_baseline(target, self.output_dir)
        provenance = [manager.provenance]
        result = SourcePreparationResult(
            source=source,
            adapter_provenance=provenance,
            synthetic=any(item.implementation_kind == "fake" for item in provenance),
        )
        return result.model_dump(mode="json")

    def handle_noop_build(self, payload: dict[str, Any]) -> dict[str, Any]:
        baseline = SourceSnapshot.model_validate(payload["baseline_source"])
        candidate = UUID(payload["candidate_id"])
        source_manager = self.adapters.require("source_manager")
        builder = self.adapters.require("builder")
        artifact_store = self.adapters.require("artifact_store")
        source = source_manager.create_candidate(baseline, candidate, self.output_dir)
        artifact = builder.build(
            {
                "candidate_id": str(candidate),
                "source_hash": source.source_hash,
                "variant": "framework-noop",
            },
            self.output_dir,
        ).model_copy(
            update={
                "source_snapshot_id": source.snapshot_id,
                "build_recipe": {
                    "kind": "framework-noop",
                    "changes": [],
                    "purpose": "validate source-to-artifact control flow",
                },
            }
        )
        artifact = artifact_store.publish(artifact, self.output_dir)
        provenance = [
            source_manager.provenance,
            builder.provenance,
            artifact_store.provenance,
        ]
        result = NoopBuildResult(
            source=source,
            artifact=artifact,
            adapter_provenance=provenance,
            synthetic=any(item.implementation_kind == "fake" for item in provenance),
        )
        return result.model_dump(mode="json")

    def handle_framework_smoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = TargetSpec.model_validate(payload["target"])
        artifact = ArtifactManifest.model_validate(payload["artifact"])
        executor = self.adapters.require("executor")
        evaluator = self.adapters.require("evaluator")
        cleaner = self.adapters.require("resource_cleaner")
        context = payload.get("_job_context", {})
        resource_id = context.get("resource_id")
        fencing_token = context.get("fencing_token")
        lease_scope = LeaseScope.EXCLUSIVE if resource_id is not None else LeaseScope.NONE
        request = ExecutionRequest(
            request_id=UUID(payload["execution_request_id"]),
            target_id=target.target_id,
            argv=[
                "python",
                "-m",
                "hcuopt.framework_smoke",
                "--artifact",
                artifact.uri,
            ],
            working_directory=target.source_baseline.clean_checkout,
            timeout_seconds=600,
            lease_scope=lease_scope,
            resource_id=resource_id,
            fencing_token=fencing_token,
            container_image=target.inference_image.immutable_reference,
        )
        execution = executor.execute(request, target, self.output_dir)
        verdict = dict(
            evaluator.correctness(
                {
                    "variant": "framework-noop",
                    "artifact": artifact.model_dump(mode="json"),
                    "baseline_reference": target.source_baseline.commit,
                },
                self.output_dir,
            )
        )
        cleanup: dict[str, Any] = {}
        if resource_id is not None and fencing_token is not None:
            cleanup["fence"] = dict(cleaner.fence(resource_id, int(fencing_token)))
        cleanup["health"] = dict(
            cleaner.health_check(resource_id or target.execution_host.name)
        )
        passed = (
            bool(verdict["passed"])
            and execution.status == "succeeded"
            and bool(cleanup["health"].get("healthy"))
        )
        provenance = [executor.provenance, evaluator.provenance, cleaner.provenance]
        synthetic = any(item.implementation_kind == "fake" for item in provenance)
        evaluation = EvaluationRun(
            evaluation_run_id=UUID(payload["evaluation_run_id"]),
            task_id=UUID(payload["task_id"]),
            candidate_id=UUID(payload["candidate_id"]),
            round_id=UUID(payload["round_id"]),
            baseline_epoch_id=UUID(payload["baseline_epoch_id"]),
            phase="correctness",
            protocol_version="framework-smoke-v1",
            target_fingerprint=payload["target_fingerprint"],
            idempotency_key=(
                f"{payload['task_id']}:framework-smoke:"
                f"{payload['retest_ordinal']}:evaluation:v1"
            ),
            passed=passed,
            metrics={
                "output_equivalent": bool(verdict["passed"]),
                "reason": verdict.get("reason", "framework output comparison"),
                "cleanup_healthy": bool(cleanup["health"].get("healthy")),
            },
            evidence_uris=[
                uri
                for uri in (execution.stdout_uri, execution.stderr_uri)
                if uri is not None
            ],
            adapter_provenance=provenance,
            synthetic=synthetic,
        )
        attempt = ExecutionAttempt(
            evaluation_run_id=evaluation.evaluation_run_id,
            request_id=request.request_id,
            attempt_number=int(context.get("attempt_number", 1)),
            status=execution.status,
            exit_code=execution.exit_code,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            stdout_uri=execution.stdout_uri,
            stderr_uri=execution.stderr_uri,
            result_metadata=execution.metadata,
            adapter_provenance=execution.adapter_provenance,
            synthetic=execution.synthetic,
        )
        evidence = EvidenceBundle(
            evidence_id=UUID(payload["evidence_id"]),
            task_id=evaluation.task_id,
            candidate_id=evaluation.candidate_id,
            baseline_epoch_id=evaluation.baseline_epoch_id,
            target_id=target.target_id,
            evidence_type="framework_smoke",
            protocol_version="framework-smoke-v1",
            artifact_ids=[artifact.artifact_id],
            summary={
                "output_equivalent": bool(verdict["passed"]),
                "execution_status": execution.status,
                "cleanup": cleanup,
                "performance_conclusion": "not_measured",
            },
            raw_uris=evaluation.evidence_uris,
            adapter_provenance=provenance,
            synthetic=synthetic,
        )
        result = FrameworkSmokeResult(
            execution_request=request,
            execution_result=execution,
            execution_attempt=attempt,
            evaluation=evaluation,
            evidence=evidence,
            cleanup_evidence=cleanup,
            adapter_provenance=provenance,
            synthetic=synthetic,
        )
        return result.model_dump(mode="json")


class FakeJobHandlers(JobHandlers):
    def __init__(self, output_dir: Path | None = None) -> None:
        super().__init__(AdapterRegistry.fake(), output_dir)


def candidate_id(payload: dict[str, Any]) -> UUID:
    return UUID(payload["candidate_id"])
