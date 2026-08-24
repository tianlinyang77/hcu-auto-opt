from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from hcuopt.adapters.interfaces import PairedFrameworkSmokeEvaluator
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.adapters.resource_cleaner import cleanup_is_healthy
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
    FrameworkSmokeVariantExecution,
    ManualCandidateBuildResult,
    NoopBuildResult,
    PairedFrameworkSmokeResult,
    SourcePreparationResult,
    Stage0ProbeResult,
)
from hcuopt.domain.enums import LeaseScope, Stage0ProbeType
from hcuopt.domain.errors import AdapterUnavailable, ExecutionSafetyError
from hcuopt.targets import target_fingerprint


class JobHandlers:
    def __init__(self, adapters: AdapterRegistry, output_dir: Path | None = None) -> None:
        self.adapters = adapters
        self.output_dir = output_dir or Path(".")

    def handle(self, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"handle_{job_type}", None)
        if handler is None:
            raise ValueError(f"unsupported job type: {job_type}")
        return handler(payload)

    def cleanup(self, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        context = payload.get("_job_context", {})
        resource_id = context.get("resource_id")
        fencing_token = context.get("fencing_token")
        if (
            job_type != "framework_smoke"
            and (resource_id is None or fencing_token is None)
        ):
            return {}
        request_ids = {
            str(value)
            for value in (
                payload.get("execution_request_id"),
                payload.get("baseline_execution_request_id"),
                payload.get("noop_execution_request_id"),
            )
            if value is not None
        }
        cancellation: dict[str, Any] | None = None
        if request_ids:
            executor = self.adapters.require("executor")
            cancellation = {
                request_id: dict(executor.cancel(UUID(request_id)))
                for request_id in sorted(request_ids)
            }
        cleanup = self._resource_cleanup(
            resource_id,
            fencing_token,
            payload.get("target"),
        )
        if cancellation is not None:
            cleanup["execution_cancel"] = cancellation
        return cleanup

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

    def handle_manual_build(self, payload: dict[str, Any]) -> dict[str, Any]:
        builder = self.adapters.require("candidate_builder")
        result = builder.build_candidate(payload, self.output_dir)
        validated = ManualCandidateBuildResult.model_validate(result)
        if builder.provenance not in validated.adapter_provenance:
            raise ExecutionSafetyError(
                "M1 build result omits the active Candidate Builder provenance"
            )
        return validated.model_dump(mode="json")

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

    def handle_stage0_probe(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = TargetSpec.model_validate(payload["target"])
        expected_target_fingerprint = target_fingerprint(target)
        if payload.get("target_fingerprint") != expected_target_fingerprint:
            raise ExecutionSafetyError("Stage 0 target fingerprint does not match TargetSpec")
        probe = self.adapters.require("stage0_probe")
        if (
            probe.provenance.implementation_kind == "real"
            and getattr(probe, "target_fingerprint", None) != expected_target_fingerprint
        ):
            raise ExecutionSafetyError("Stage 0 probe adapter is bound to a different target")
        output = probe.run_probe(payload, self.output_dir)
        result = Stage0ProbeResult(
            stage0_run_id=UUID(payload["stage0_run_id"]),
            target_snapshot_id=UUID(payload["target_snapshot_id"]),
            probe_type=Stage0ProbeType(payload["probe_type"]),
            protocol_version=payload["protocol_version"],
            raw_evidence_uri=output.raw_evidence_uri,
            raw_evidence_hash=output.raw_evidence_hash,
            summary=output.summary,
            adapter_provenance=list(
                output.adapter_provenance or (probe.provenance,)
            ),
            synthetic=output.synthetic,
            cleanup_evidence=output.cleanup_evidence,
        )
        return result.model_dump(mode="json")

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
        try:
            artifact = builder.build(
                {
                    "candidate_id": str(candidate),
                    "source_hash": source.source_hash,
                    "source_snapshot": source.model_dump(mode="json"),
                    "adapter_provenance": [
                        source_manager.provenance.model_dump(mode="json")
                    ],
                    "variant": "framework-noop",
                },
                self.output_dir,
            )
            artifact = artifact.model_copy(
                update={
                    "source_snapshot_id": source.snapshot_id,
                    "build_recipe": {
                        **artifact.build_recipe,
                        "workflow_kind": "framework-noop",
                        "changes": [],
                        "purpose": "validate source-to-artifact control flow",
                    },
                }
            )
            artifact = artifact_store.publish(artifact, self.output_dir)
        finally:
            source_manager.remove_candidate(baseline, source, self.output_dir)
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
        if evaluator.provenance.implementation_kind == "real":
            if not isinstance(evaluator, PairedFrameworkSmokeEvaluator):
                raise AdapterUnavailable(
                    "real Framework Smoke evaluator lacks the paired execution interface"
                )
            return self._handle_paired_framework_smoke(
                payload,
                target,
                artifact,
                executor,
                evaluator,
                cleaner,
            )
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
        try:
            execution = executor.execute(request, target, self.output_dir)
            if execution.status == "succeeded":
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
            else:
                verdict = {
                    "passed": False,
                    "reason": f"execution ended with status {execution.status}",
                }
        finally:
            cleanup = self._resource_cleanup(
                resource_id,
                fencing_token,
                target.model_dump(mode="json"),
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

    def _handle_paired_framework_smoke(
        self,
        payload: dict[str, Any],
        target: TargetSpec,
        artifact: ArtifactManifest,
        executor: Any,
        evaluator: PairedFrameworkSmokeEvaluator,
        cleaner: Any,
    ) -> dict[str, Any]:
        context = payload.get("_job_context", {})
        resource_id = context.get("resource_id")
        fencing_token = context.get("fencing_token")
        if resource_id is None or fencing_token is None:
            raise ExecutionSafetyError(
                "paired Framework Smoke requires an exclusive resource and fencing token"
            )
        self._require_live_lease(context)
        task_id = UUID(payload["task_id"])
        evaluation_run_id = UUID(payload["evaluation_run_id"])
        attempt_number = int(context.get("attempt_number", 1))
        plan = evaluator.prepare_framework_smoke(
            target=target,
            artifact=artifact,
            output_dir=self.output_dir,
            task_id=task_id,
            evaluation_run_id=evaluation_run_id,
            attempt_number=attempt_number,
            baseline_request_id=UUID(payload["baseline_execution_request_id"]),
            noop_request_id=UUID(payload["noop_execution_request_id"]),
            resource_id=str(resource_id),
            fencing_token=int(fencing_token),
        )

        try:
            baseline_execution = executor.execute(
                plan.baseline_request, target, self.output_dir
            )
        finally:
            baseline_cleanup = self._resource_cleanup(
                str(resource_id),
                int(fencing_token),
                target.model_dump(mode="json"),
            )
        if not cleanup_is_healthy(baseline_cleanup):
            raise ExecutionSafetyError(
                "baseline cleanup or health check failed; refusing to launch no-op"
            )
        self._require_live_lease(context)
        try:
            noop_execution = executor.execute(plan.noop_request, target, self.output_dir)
        finally:
            noop_cleanup = self._resource_cleanup(
                str(resource_id),
                int(fencing_token),
                target.model_dump(mode="json"),
            )

        provenance = [executor.provenance, evaluator.provenance, cleaner.provenance]
        outcome = evaluator.evaluate_framework_smoke(
            plan,
            task_id=task_id,
            candidate_id=UUID(payload["candidate_id"]),
            round_id=UUID(payload["round_id"]),
            baseline_epoch_id=UUID(payload["baseline_epoch_id"]),
            target=target,
            target_fingerprint=payload["target_fingerprint"],
            artifact=artifact,
            evaluation_run_id=evaluation_run_id,
            evidence_id=UUID(payload["evidence_id"]),
            attempt_number=attempt_number,
            baseline_execution=baseline_execution,
            noop_execution=noop_execution,
            baseline_cleanup=baseline_cleanup,
            noop_cleanup=noop_cleanup,
            adapter_provenance=provenance,
            idempotency_key=(
                f"{payload['task_id']}:framework-smoke:"
                f"{payload['retest_ordinal']}:evaluation:v1"
            ),
        )
        synthetic = any(item.implementation_kind == "fake" for item in provenance)
        cleanup_evidence = {
            **noop_cleanup,
            "variants": {
                "baseline": baseline_cleanup,
                "noop": noop_cleanup,
            },
        }
        result = PairedFrameworkSmokeResult(
            executions=[
                FrameworkSmokeVariantExecution(
                    variant="baseline",
                    execution_request=plan.baseline_request,
                    execution_result=baseline_execution,
                    execution_attempt=outcome.baseline_attempt,
                    cleanup_evidence=baseline_cleanup,
                ),
                FrameworkSmokeVariantExecution(
                    variant="noop",
                    execution_request=plan.noop_request,
                    execution_result=noop_execution,
                    execution_attempt=outcome.noop_attempt,
                    cleanup_evidence=noop_cleanup,
                ),
            ],
            evaluation=outcome.artifacts.evaluation,
            evidence=outcome.artifacts.evidence,
            cleanup_evidence=cleanup_evidence,
            adapter_provenance=provenance,
            synthetic=synthetic,
        )
        return result.model_dump(mode="json")

    def _resource_cleanup(
        self,
        resource_id: str | None,
        fencing_token: int | None,
        raw_target: dict[str, Any] | None,
    ) -> dict[str, Any]:
        cleaner = self.adapters.require("resource_cleaner")
        target_name = (
            TargetSpec.model_validate(raw_target).execution_host.name
            if raw_target is not None
            else "unknown-target"
        )
        cleanup: dict[str, Any] = {}
        if resource_id is not None and fencing_token is not None:
            try:
                cleanup["fence"] = dict(
                    cleaner.fence(resource_id, int(fencing_token))
                )
            except Exception as exc:
                cleanup["fence"] = {
                    "resource_id": resource_id,
                    "fencing_token": fencing_token,
                    "fenced": False,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
        else:
            cleanup["fence"] = {
                "resource_id": resource_id,
                "fencing_token": fencing_token,
                "fenced": True,
                "not_required": True,
            }
        try:
            cleanup["health"] = dict(
                cleaner.health_check(resource_id or target_name)
            )
        except Exception as exc:
            cleanup["health"] = {
                "resource_id": resource_id or target_name,
                "healthy": False,
                "quarantined": True,
                "error": f"{exc.__class__.__name__}: {exc}",
            }
        return cleanup

    @staticmethod
    def _require_live_lease(context: dict[str, Any]) -> None:
        lease_lost = context.get("lease_lost_event")
        if lease_lost is not None and lease_lost.is_set():
            raise ExecutionSafetyError("job lease was lost before the next container launch")


class FakeJobHandlers(JobHandlers):
    def __init__(self, output_dir: Path | None = None) -> None:
        super().__init__(AdapterRegistry.fake(), output_dir)


def candidate_id(payload: dict[str, Any]) -> UUID:
    return UUID(payload["candidate_id"])
