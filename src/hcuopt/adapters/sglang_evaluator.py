from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from hcuopt.adapters.resource_cleaner import cleanup_is_healthy
from hcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    ArtifactManifest,
    ExecutionAttempt,
    ExecutionRequest,
    ExecutionResult,
    MountSpec,
    TargetSpec,
)
from hcuopt.evaluation.sglang_smoke import (
    EquivalenceResult,
    NormalizedSGLangOutput,
    SmokeEvidenceArtifacts,
    SmokeVariantSpec,
    build_evidence,
    build_execution_request,
    build_failed_comparison,
    compare_outputs,
    load_workload_spec,
    validate_variant_pair,
    write_evidence_artifacts,
    write_workload_spec,
)
from hcuopt.source_hash import file_uri_to_path


@dataclass(frozen=True, slots=True)
class SGLangSmokePlan:
    evidence_root: Path
    baseline_evidence_dir: Path
    noop_evidence_dir: Path
    baseline_request: ExecutionRequest
    noop_request: ExecutionRequest


@dataclass(frozen=True, slots=True)
class SGLangSmokeEvaluation:
    baseline_attempt: ExecutionAttempt
    noop_attempt: ExecutionAttempt
    comparison: EquivalenceResult
    artifacts: SmokeEvidenceArtifacts


class SGLangSmokeEvaluator:
    """Build and judge the two fresh-container SGLang Framework Smoke variants."""

    def __init__(
        self,
        *,
        profile: str = "nmz36-framework-smoke-v1",
        workload_path: Path | None = None,
        runner_host_path: Path | None = None,
    ) -> None:
        project_root = Path(__file__).resolve().parents[3]
        self.workload_path = (
            workload_path or project_root / "config" / "workloads" / "nmz36-sglang-smoke-v1.yaml"
        ).resolve()
        self.runner_host_path = (
            runner_host_path
            or project_root / "src" / "hcuopt" / "evaluation" / "sglang_smoke_runner.py"
        ).resolve()
        self.provenance = AdapterProvenance(
            profile=profile,
            capability="evaluator",
            adapter_name=type(self).__name__,
            adapter_version="1.0.0",
            implementation_kind="real",
        )

    def correctness(self, plan: Mapping[str, Any], output_dir: Path) -> Mapping[str, Any]:
        raise ValueError("SGLangSmokeEvaluator requires the paired Framework Smoke interface")

    def e2e(self, plan: Mapping[str, Any], output_dir: Path) -> Mapping[str, Any]:
        raise ValueError("Framework Smoke does not produce an E2E performance verdict")

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
    ) -> SGLangSmokePlan:
        if attempt_number < 1:
            raise ValueError("Framework Smoke attempt_number must be positive")
        workload = load_workload_spec(self.workload_path)
        root = (
            output_dir.resolve()
            / "framework-smoke"
            / str(task_id)
            / str(evaluation_run_id)
            / f"attempt-{attempt_number:04d}"
        )
        root.mkdir(parents=True, exist_ok=False)
        input_dir = root / "input"
        baseline_dir = root / "baseline"
        noop_dir = root / "noop"
        input_dir.mkdir()
        baseline_dir.mkdir()
        noop_dir.mkdir()
        spec_path = input_dir / "spec.json"
        write_workload_spec(spec_path, workload)

        if artifact.kind != "noop-source-archive":
            raise ValueError("Framework Smoke requires a no-op source archive")
        if artifact.candidate_id is None:
            raise ValueError("no-op artifact must identify its candidate")
        artifact_source = file_uri_to_path(artifact.uri)
        if artifact_source.is_symlink():
            raise ValueError("no-op artifact cannot be a symbolic link")
        artifact_path = artifact_source.resolve(strict=True)
        if not artifact_path.is_file():
            raise ValueError("no-op artifact must be a regular file")
        actual_artifact_hash = _sha256_file(artifact_path)
        if actual_artifact_hash != artifact.content_hash:
            raise ValueError(
                "no-op artifact content hash does not match ArtifactManifest: "
                f"{actual_artifact_hash} != {artifact.content_hash}"
            )
        runner_path = self.runner_host_path.resolve(strict=True)
        baseline = SmokeVariantSpec(
            name="baseline",
            runner_host_path=runner_path.as_posix(),
            spec_host_path=spec_path.as_posix(),
            evidence_host_dir=baseline_dir.as_posix(),
        )
        noop = SmokeVariantSpec(
            name="noop",
            artifact_id=artifact.artifact_id,
            runner_host_path=runner_path.as_posix(),
            spec_host_path=spec_path.as_posix(),
            evidence_host_dir=noop_dir.as_posix(),
            mounts=[
                MountSpec(
                    source=artifact_path.as_posix(),
                    target="/opt/hcuopt/artifacts/noop-source.tar",
                    read_only=True,
                )
            ],
        )
        validate_variant_pair(baseline, noop, artifact)
        return SGLangSmokePlan(
            evidence_root=root,
            baseline_evidence_dir=baseline_dir,
            noop_evidence_dir=noop_dir,
            baseline_request=build_execution_request(
                target,
                workload,
                baseline,
                request_id=baseline_request_id,
                resource_id=resource_id,
                fencing_token=fencing_token,
            ),
            noop_request=build_execution_request(
                target,
                workload,
                noop,
                artifact,
                request_id=noop_request_id,
                resource_id=resource_id,
                fencing_token=fencing_token,
            ),
        )

    def evaluate_framework_smoke(
        self,
        plan: SGLangSmokePlan,
        *,
        task_id: UUID,
        candidate_id: UUID,
        round_id: UUID,
        baseline_epoch_id: UUID,
        target: TargetSpec,
        target_fingerprint: str,
        artifact: ArtifactManifest,
        evaluation_run_id: UUID,
        evidence_id: UUID,
        attempt_number: int,
        baseline_execution: ExecutionResult,
        noop_execution: ExecutionResult,
        baseline_cleanup: Mapping[str, Any],
        noop_cleanup: Mapping[str, Any],
        adapter_provenance: Sequence[AdapterProvenance],
        idempotency_key: str,
    ) -> SGLangSmokeEvaluation:
        baseline_attempt = self._attempt(
            "baseline", evaluation_run_id, attempt_number, baseline_execution
        )
        noop_attempt = self._attempt("noop", evaluation_run_id, attempt_number, noop_execution)
        baseline_output, baseline_error = self._variant_output(
            "baseline", plan.baseline_evidence_dir, baseline_execution
        )
        noop_output, noop_error = self._variant_output(
            "noop", plan.noop_evidence_dir, noop_execution
        )
        comparison = self._comparison(
            baseline_output,
            noop_output,
            baseline_error,
            noop_error,
        )
        cleanup_healthy = cleanup_is_healthy(baseline_cleanup) and cleanup_is_healthy(noop_cleanup)
        raw_uris = [
            uri
            for uri in (
                baseline_execution.stdout_uri,
                baseline_execution.stderr_uri,
                noop_execution.stdout_uri,
                noop_execution.stderr_uri,
                (plan.baseline_evidence_dir / "result.json").resolve().as_uri(),
                (plan.noop_evidence_dir / "result.json").resolve().as_uri(),
            )
            if uri is not None
        ]
        evidence_root_uri = plan.evidence_root.resolve().as_uri()
        sha256_manifest_uri = (plan.evidence_root / "sha256sums.json").resolve().as_uri()
        artifacts = build_evidence(
            task_id=task_id,
            candidate_id=candidate_id,
            round_id=round_id,
            baseline_epoch_id=baseline_epoch_id,
            target=target,
            target_fingerprint=target_fingerprint,
            artifact=artifact,
            comparison=comparison,
            adapter_provenance=adapter_provenance,
            idempotency_key=idempotency_key,
            baseline_execution_attempt_id=baseline_attempt.execution_attempt_id,
            noop_execution_attempt_id=noop_attempt.execution_attempt_id,
            evidence_root_uri=evidence_root_uri,
            sha256_manifest_uri=sha256_manifest_uri,
            raw_uris=raw_uris,
            baseline_execution_succeeded=baseline_execution.status == "succeeded",
            noop_execution_succeeded=noop_execution.status == "succeeded",
            cleanup_healthy=cleanup_healthy,
            evaluation_run_id=evaluation_run_id,
            evidence_id=evidence_id,
        )
        write_evidence_artifacts(plan.evidence_root, comparison, artifacts)
        return SGLangSmokeEvaluation(
            baseline_attempt=baseline_attempt,
            noop_attempt=noop_attempt,
            comparison=comparison,
            artifacts=artifacts,
        )

    @staticmethod
    def _attempt(
        variant: str,
        evaluation_run_id: UUID,
        attempt_number: int,
        execution: ExecutionResult,
    ) -> ExecutionAttempt:
        return ExecutionAttempt(
            evaluation_run_id=evaluation_run_id,
            request_id=execution.request_id,
            variant=variant,
            attempt_number=attempt_number,
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

    @staticmethod
    def _variant_output(
        variant: str,
        evidence_dir: Path,
        execution: ExecutionResult,
    ) -> tuple[NormalizedSGLangOutput | None, str | None]:
        if execution.status != "succeeded":
            return None, f"container execution ended with status {execution.status}"
        result_path = evidence_dir / "result.json"
        try:
            raw = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return None, f"{variant} result.json is unavailable: {exc}"
        if not isinstance(raw, dict):
            return None, f"{variant} result.json is not an object"
        if raw.get("status") != "succeeded":
            error = raw.get("error")
            return None, json.dumps(error, ensure_ascii=False, sort_keys=True)
        try:
            return NormalizedSGLangOutput.model_validate(raw.get("normalized_output")), None
        except ValueError as exc:
            return None, f"{variant} normalized output is invalid: {exc}"

    @staticmethod
    def _comparison(
        baseline: NormalizedSGLangOutput | None,
        noop: NormalizedSGLangOutput | None,
        baseline_error: str | None,
        noop_error: str | None,
    ) -> EquivalenceResult:
        if baseline is not None and noop is not None:
            return compare_outputs(baseline, noop)
        return build_failed_comparison(
            baseline=baseline,
            noop=noop,
            baseline_error=baseline_error,
            noop_error=noop_error,
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"
