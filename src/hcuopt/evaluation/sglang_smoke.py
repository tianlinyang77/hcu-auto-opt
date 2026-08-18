from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import unquote, urlsplit
from uuid import UUID

import yaml
from pydantic import ConfigDict, Field, StrictInt, field_validator, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    ArtifactManifest,
    EvaluationRun,
    EvidenceBundle,
    ExecutionRequest,
    MountSpec,
    TargetSpec,
)
from hcuopt.domain.enums import LeaseScope

SGLANG_SMOKE_PROTOCOL_VERSION = "sglang-smoke-v1"
FRAMEWORK_SMOKE_PROTOCOL_VERSION = "framework-smoke-v1"
VARIANT_EVIDENCE_FILES = frozenset(
    {
        "spec.json",
        "environment.json",
        "start.json",
        "server.log",
        "ready.jsonl",
        "request.json",
        "response.json",
        "stop.json",
        "result.json",
    }
)
NO_PERFORMANCE_CONCLUSION = (
    "本结果仅证明 Framework 功能与 Baseline/No-op 等价性，不构成性能结论。"
)


class SGLangResponseError(ValueError):
    """The SGLang response cannot be used for strict equivalence."""


class SGLangWorkloadSpec(ContractModel):
    """D-owned deterministic workload; it is not a public platform contract."""

    model_config = ConfigDict(extra="forbid", strict=True)

    protocol_version: Literal["sglang-smoke-v1"] = SGLANG_SMOKE_PROTOCOL_VERSION
    workload_id: str = Field(min_length=1, max_length=200)
    target_id: str = Field(min_length=1, max_length=128)
    model_path: str = Field(min_length=1)
    served_model_name: str = Field(default="hcuopt-smoke", min_length=1)
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=30_000, ge=1, le=65_535)
    tensor_parallel_size: int = Field(default=1, ge=1)
    prompt: str = Field(min_length=1)
    temperature: float = 0.0
    max_new_tokens: int = Field(default=8, ge=1, le=4_096)
    sampling_seed: int = Field(default=0, ge=0)
    stream: bool = False
    ready_path: Literal["/health_generate"] = "/health_generate"
    generate_path: Literal["/generate"] = "/generate"
    ready_timeout_seconds: float = Field(default=300.0, gt=0, le=1_800)
    ready_poll_interval_seconds: float = Field(default=1.0, gt=0, le=30)
    request_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    stop_grace_seconds: float = Field(default=10.0, gt=0, le=120)
    execution_timeout_seconds: int = Field(default=420, ge=1, le=86_400)
    python_executable: Literal["python"] = "python"
    server_module: Literal["sglang.launch_server"] = "sglang.launch_server"

    @field_validator("model_path")
    @classmethod
    def validate_model_path(cls, value: str) -> str:
        return _validate_absolute_posix_path(value, "model_path")

    @field_validator("temperature")
    @classmethod
    def require_deterministic_temperature(cls, value: float) -> float:
        if value != 0.0:
            raise ValueError("framework smoke requires temperature=0.0")
        return value

    @field_validator("stream")
    @classmethod
    def require_non_streaming_response(cls, value: bool) -> bool:
        if value:
            raise ValueError("framework smoke requires stream=false")
        return value

    @model_validator(mode="after")
    def validate_timeout_budget(self) -> SGLangWorkloadSpec:
        required = (
            self.ready_timeout_seconds
            + self.request_timeout_seconds
            + self.stop_grace_seconds
        )
        if self.execution_timeout_seconds <= required:
            raise ValueError(
                "execution_timeout_seconds must exceed ready, request, and stop timeouts"
            )
        return self

    def server_argv(self) -> list[str]:
        return [
            self.python_executable,
            "-m",
            self.server_module,
            "--model-path",
            self.model_path,
            "--served-model-name",
            self.served_model_name,
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--tp-size",
            str(self.tensor_parallel_size),
        ]

    def request_payload(self) -> dict[str, Any]:
        return {
            "text": self.prompt,
            "sampling_params": {
                "temperature": self.temperature,
                "max_new_tokens": self.max_new_tokens,
                "sampling_seed": self.sampling_seed,
            },
            "stream": self.stream,
        }


class SmokeVariantSpec(ContractModel):
    """Resolved host/container paths for one fresh baseline or no-op execution."""

    name: Literal["baseline", "noop"]
    runner_host_path: str
    spec_host_path: str
    evidence_host_dir: str
    artifact_id: UUID | None = None
    mounts: list[MountSpec] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)
    runner_container_path: Literal[
        "/opt/hcuopt/sglang_smoke_runner.py"
    ] = "/opt/hcuopt/sglang_smoke_runner.py"
    spec_container_path: Literal["/work/input/spec.json"] = "/work/input/spec.json"
    evidence_container_dir: Literal["/work/output"] = "/work/output"
    working_directory: Literal["/work"] = "/work"

    @field_validator(
        "runner_host_path",
        "spec_host_path",
        "evidence_host_dir",
        "runner_container_path",
        "spec_container_path",
        "evidence_container_dir",
        "working_directory",
    )
    @classmethod
    def require_absolute_paths(cls, value: str) -> str:
        return _validate_absolute_posix_path(value, "smoke path")

    @model_validator(mode="after")
    def validate_artifact_binding(self) -> SmokeVariantSpec:
        if self.name == "baseline" and self.artifact_id is not None:
            raise ValueError("baseline variant cannot bind a candidate artifact")
        if self.name == "baseline" and self.mounts:
            raise ValueError("baseline variant cannot mount a candidate artifact")
        if self.name == "noop" and self.artifact_id is None:
            raise ValueError("no-op variant requires an artifact_id")
        if self.name == "noop" and len(self.mounts) != 1:
            raise ValueError("no-op variant requires exactly one artifact mount")
        if self.name == "noop" and not self.mounts[0].read_only:
            raise ValueError("no-op artifact mount must be read-only")
        return self


class NormalizedSGLangOutput(ContractModel):
    text: str
    finish_reason_type: str = Field(min_length=1)
    prompt_tokens: StrictInt = Field(ge=0)
    completion_tokens: StrictInt = Field(ge=0)


class ComparisonDifference(ContractModel):
    baseline: Any
    noop: Any


class EquivalenceResult(ContractModel):
    passed: bool
    baseline: NormalizedSGLangOutput | None = None
    noop: NormalizedSGLangOutput | None = None
    differences: dict[str, ComparisonDifference] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)
    baseline_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    noop_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_comparison(self) -> EquivalenceResult:
        if set(self.errors) - {"baseline", "noop"}:
            raise ValueError("comparison errors may only identify baseline or noop")
        for name, output, digest in (
            ("baseline", self.baseline, self.baseline_hash),
            ("noop", self.noop, self.noop_hash),
        ):
            if output is None and digest is not None:
                raise ValueError(f"{name}_hash requires a normalized output")
            if output is not None and digest != normalized_output_hash(output):
                raise ValueError(f"{name}_hash does not match normalized output")
            if output is None and name not in self.errors:
                raise ValueError(f"missing {name} output requires a structured error")
        if self.passed and (self.differences or self.errors):
            raise ValueError("a passing comparison cannot contain differences or errors")
        if self.passed and (self.baseline is None or self.noop is None):
            raise ValueError("a passing comparison requires both normalized outputs")
        if not self.passed and not self.differences and not self.errors:
            raise ValueError("a failing comparison requires differences or errors")
        return self


class SmokeEvidenceArtifacts(ContractModel):
    evaluation: EvaluationRun
    evidence: EvidenceBundle
    report_markdown: str


def write_workload_spec(path: Path, workload: SGLangWorkloadSpec) -> None:
    _atomic_write_json(path, workload.model_dump(mode="json"))


def load_workload_spec(path: Path) -> SGLangWorkloadSpec:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"workload spec {path} must contain one mapping")
    return SGLangWorkloadSpec.model_validate(raw)


def validate_variant_pair(
    baseline: SmokeVariantSpec,
    noop: SmokeVariantSpec,
    artifact: ArtifactManifest,
) -> None:
    if baseline.name != "baseline" or noop.name != "noop":
        raise ValueError("variant pair must be ordered baseline then no-op")
    _validate_noop_artifact_binding(noop, artifact)
    equal_fields = (
        "runner_host_path",
        "spec_host_path",
        "environment",
        "runner_container_path",
        "spec_container_path",
        "evidence_container_dir",
        "working_directory",
    )
    changed = [
        field
        for field in equal_fields
        if getattr(baseline, field) != getattr(noop, field)
    ]
    if changed:
        raise ValueError(
            "baseline and no-op differ outside the artifact mount: "
            + ", ".join(changed)
        )
    if baseline.evidence_host_dir == noop.evidence_host_dir:
        raise ValueError("baseline and no-op require separate evidence directories")


def _validate_noop_artifact_binding(
    noop: SmokeVariantSpec,
    artifact: ArtifactManifest,
) -> None:
    if noop.artifact_id != artifact.artifact_id:
        raise ValueError("no-op variant artifact_id does not match ArtifactManifest")
    artifact_uri = urlsplit(artifact.uri)
    if artifact_uri.scheme == "file":
        artifact_path = unquote(artifact_uri.path)
        if artifact_uri.netloc or noop.mounts[0].source != artifact_path:
            raise ValueError("no-op artifact mount source does not match artifact URI")


def build_execution_request(
    target: TargetSpec,
    workload: SGLangWorkloadSpec,
    variant: SmokeVariantSpec,
    artifact: ArtifactManifest | None = None,
    *,
    request_id: UUID | None = None,
    resource_id: str | None = None,
    fencing_token: int | None = None,
) -> ExecutionRequest:
    """Build one self-contained runner request for one fresh container."""

    if workload.target_id != target.target_id:
        raise ValueError(
            f"workload target mismatch: {workload.target_id} != {target.target_id}"
        )
    if (resource_id is None) != (fencing_token is None):
        raise ValueError("resource_id and fencing_token must be supplied together")
    if variant.name == "baseline" and artifact is not None:
        raise ValueError("baseline execution cannot bind an ArtifactManifest")
    if variant.name == "baseline" and variant.mounts:
        raise ValueError("baseline execution cannot contain an artifact mount")
    if variant.name == "noop" and len(variant.mounts) != 1:
        raise ValueError("no-op execution requires exactly one artifact mount")
    if variant.name == "noop" and (
        artifact is None or variant.artifact_id != artifact.artifact_id
    ):
        raise ValueError("no-op execution requires its matching ArtifactManifest")
    if variant.name == "noop":
        assert artifact is not None
        _validate_noop_artifact_binding(variant, artifact)

    work_root = PurePosixPath(target.execution_host.work_root)
    prohibited = PurePosixPath(target.execution_host.prohibited_work_root)
    _validate_absolute_posix_path(str(work_root), "target work_root")
    _validate_absolute_posix_path(str(prohibited), "target prohibited_work_root")
    for label, value in (
        ("runner_host_path", variant.runner_host_path),
        ("spec_host_path", variant.spec_host_path),
        ("evidence_host_dir", variant.evidence_host_dir),
    ):
        path = PurePosixPath(value)
        if not path.is_relative_to(work_root):
            raise ValueError(f"{label} must stay under target work_root {work_root}")
        if path == prohibited or path.is_relative_to(prohibited):
            raise ValueError(f"{label} cannot use prohibited work root {prohibited}")

    reserved_mounts = [
        MountSpec(
            source=variant.runner_host_path,
            target=variant.runner_container_path,
            read_only=True,
        ),
        MountSpec(
            source=variant.spec_host_path,
            target=variant.spec_container_path,
            read_only=True,
        ),
        MountSpec(
            source=variant.evidence_host_dir,
            target=variant.evidence_container_dir,
            read_only=False,
        ),
        MountSpec(
            source=workload.model_path,
            target=workload.model_path,
            read_only=True,
        ),
    ]
    mounts = [
        *reserved_mounts,
        *target.execution_host.runtime_mounts,
        *variant.mounts,
    ]
    for mount in mounts:
        _validate_absolute_posix_path(mount.source, "mount source")
        _validate_absolute_posix_path(mount.target, "mount target")
    targets = [PurePosixPath(item.target) for item in mounts]
    for index, target_path in enumerate(targets):
        for other in targets[index + 1 :]:
            if target_path.is_relative_to(other) or other.is_relative_to(target_path):
                raise ValueError("execution request contains overlapping mount targets")

    values: dict[str, Any] = {
        "target_id": target.target_id,
        "argv": [
            workload.python_executable,
            variant.runner_container_path,
            "--spec",
            variant.spec_container_path,
            "--evidence-dir",
            variant.evidence_container_dir,
        ],
        "working_directory": variant.working_directory,
        "environment": {**variant.environment, "PYTHONUNBUFFERED": "1"},
        "timeout_seconds": workload.execution_timeout_seconds,
        "lease_scope": (
            LeaseScope.EXCLUSIVE if resource_id is not None else LeaseScope.NONE
        ),
        "resource_id": resource_id,
        "fencing_token": fencing_token,
        "container_image": target.inference_image.immutable_reference,
        "mounts": mounts,
    }
    if request_id is not None:
        values["request_id"] = request_id
    return ExecutionRequest.model_validate(values)


def normalize_response(
    response: Mapping[str, Any] | str | bytes,
) -> NormalizedSGLangOutput:
    """Extract only deterministic fields; no text trimming or fuzzy comparison."""

    if isinstance(response, bytes):
        try:
            response = response.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SGLangResponseError("response is not valid UTF-8") from exc
    if isinstance(response, str):
        try:
            parsed = json.loads(response)
        except json.JSONDecodeError as exc:
            raise SGLangResponseError("response is not valid JSON") from exc
    elif isinstance(response, Mapping):
        parsed = dict(response)
    else:
        raise SGLangResponseError("response must be a JSON object")
    if not isinstance(parsed, dict):
        raise SGLangResponseError("response must be a JSON object")

    text = parsed.get("text")
    meta = parsed.get("meta_info")
    if not isinstance(text, str):
        raise SGLangResponseError("response.text must be a string")
    if not isinstance(meta, dict):
        raise SGLangResponseError("response.meta_info must be an object")
    finish_reason = meta.get("finish_reason")
    if not isinstance(finish_reason, dict):
        raise SGLangResponseError("response.meta_info.finish_reason must be an object")
    finish_type = finish_reason.get("type")
    if not isinstance(finish_type, str) or not finish_type:
        raise SGLangResponseError("response finish_reason.type must be a non-empty string")

    prompt_tokens = meta.get("prompt_tokens")
    completion_tokens = meta.get("completion_tokens")
    for name, value in (
        ("prompt_tokens", prompt_tokens),
        ("completion_tokens", completion_tokens),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SGLangResponseError(f"response meta_info.{name} must be a non-negative int")

    return NormalizedSGLangOutput(
        text=text,
        finish_reason_type=finish_type,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def normalized_output_hash(output: NormalizedSGLangOutput) -> str:
    payload = json.dumps(
        output.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def compare_outputs(
    baseline: NormalizedSGLangOutput,
    noop: NormalizedSGLangOutput,
) -> EquivalenceResult:
    differences: dict[str, ComparisonDifference] = {}
    baseline_values = baseline.model_dump(mode="json")
    noop_values = noop.model_dump(mode="json")
    for field in baseline_values:
        if baseline_values[field] != noop_values[field]:
            differences[field] = ComparisonDifference(
                baseline=baseline_values[field],
                noop=noop_values[field],
            )
    return EquivalenceResult(
        passed=not differences,
        baseline=baseline,
        noop=noop,
        differences=differences,
        baseline_hash=normalized_output_hash(baseline),
        noop_hash=normalized_output_hash(noop),
    )


def build_failed_comparison(
    *,
    baseline: NormalizedSGLangOutput | None = None,
    noop: NormalizedSGLangOutput | None = None,
    baseline_error: str | None = None,
    noop_error: str | None = None,
) -> EquivalenceResult:
    errors = {
        name: error
        for name, error in (("baseline", baseline_error), ("noop", noop_error))
        if error
    }
    return EquivalenceResult(
        passed=False,
        baseline=baseline,
        noop=noop,
        errors=errors,
        baseline_hash=(normalized_output_hash(baseline) if baseline is not None else None),
        noop_hash=normalized_output_hash(noop) if noop is not None else None,
    )


def build_evidence(
    *,
    task_id: UUID,
    candidate_id: UUID,
    round_id: UUID,
    baseline_epoch_id: UUID,
    target: TargetSpec,
    target_fingerprint: str,
    artifact: ArtifactManifest,
    comparison: EquivalenceResult,
    adapter_provenance: Sequence[AdapterProvenance],
    idempotency_key: str,
    baseline_execution_attempt_id: UUID,
    noop_execution_attempt_id: UUID,
    evidence_root_uri: str,
    sha256_manifest_uri: str,
    raw_uris: Sequence[str] = (),
    baseline_execution_succeeded: bool,
    noop_execution_succeeded: bool,
    cleanup_healthy: bool,
    evaluation_run_id: UUID | None = None,
    evidence_id: UUID | None = None,
) -> SmokeEvidenceArtifacts:
    provenance = list(adapter_provenance)
    if not provenance:
        raise ValueError("framework smoke evidence requires adapter provenance")
    capabilities = {item.capability for item in provenance}
    missing_capabilities = {"executor", "evaluator"} - capabilities
    if missing_capabilities:
        names = ", ".join(sorted(missing_capabilities))
        raise ValueError(f"framework smoke provenance is missing: {names}")
    if artifact.candidate_id != candidate_id:
        raise ValueError("artifact candidate_id must match evaluation candidate_id")
    if baseline_execution_attempt_id == noop_execution_attempt_id:
        raise ValueError("baseline and no-op require distinct execution attempt IDs")
    if not evidence_root_uri or not sha256_manifest_uri:
        raise ValueError("framework smoke requires evidence root and manifest URIs")
    evidence_uris = sorted(
        set((*raw_uris, evidence_root_uri, sha256_manifest_uri))
    )
    if not (
        target_fingerprint.startswith("sha256:")
        and len(target_fingerprint) == len("sha256:") + 64
        and all(character in "0123456789abcdef" for character in target_fingerprint[7:])
    ):
        raise ValueError("target_fingerprint must be a lowercase SHA-256 digest")
    expected_target_fingerprint = _target_fingerprint(target)
    if target_fingerprint != expected_target_fingerprint:
        raise ValueError("target_fingerprint does not match TargetSpec")
    synthetic = artifact.synthetic or any(
        item.implementation_kind == "fake" for item in provenance
    )
    passed = all(
        (
            comparison.passed,
            baseline_execution_succeeded,
            noop_execution_succeeded,
            cleanup_healthy,
        )
    )
    evaluation_values: dict[str, Any] = {
        "task_id": task_id,
        "candidate_id": candidate_id,
        "round_id": round_id,
        "baseline_epoch_id": baseline_epoch_id,
        "phase": "correctness",
        "protocol_version": FRAMEWORK_SMOKE_PROTOCOL_VERSION,
        "target_fingerprint": target_fingerprint,
        "idempotency_key": idempotency_key,
        "passed": passed,
        "metrics": {
            "output_equivalent": comparison.passed,
            "baseline_execution_succeeded": baseline_execution_succeeded,
            "noop_execution_succeeded": noop_execution_succeeded,
            "cleanup_healthy": cleanup_healthy,
            "difference_count": len(comparison.differences),
            "comparison_errors": comparison.errors,
        },
        "measurement": None,
        "evidence_uris": evidence_uris,
        "adapter_provenance": provenance,
        "synthetic": synthetic,
    }
    if evaluation_run_id is not None:
        evaluation_values["evaluation_run_id"] = evaluation_run_id
    evaluation = EvaluationRun.model_validate(evaluation_values)

    evidence_values: dict[str, Any] = {
        "task_id": task_id,
        "candidate_id": candidate_id,
        "baseline_epoch_id": baseline_epoch_id,
        "target_id": target.target_id,
        "evidence_type": "framework_smoke",
        "protocol_version": FRAMEWORK_SMOKE_PROTOCOL_VERSION,
        "artifact_ids": [artifact.artifact_id],
        "measurement_ids": [],
        "summary": {
            "evaluation_run_id": str(evaluation.evaluation_run_id),
            "execution_attempt_ids": {
                "baseline": str(baseline_execution_attempt_id),
                "noop": str(noop_execution_attempt_id),
            },
            "output_equivalent": comparison.passed,
            "baseline_output_hash": comparison.baseline_hash,
            "noop_output_hash": comparison.noop_hash,
            "comparison_errors": comparison.errors,
            "baseline_execution_succeeded": baseline_execution_succeeded,
            "noop_execution_succeeded": noop_execution_succeeded,
            "cleanup_healthy": cleanup_healthy,
            "artifact_content_hash": artifact.content_hash,
            "evidence_root_uri": evidence_root_uri,
            "sha256_manifest_uri": sha256_manifest_uri,
            "performance_conclusion": "not_measured",
        },
        "raw_uris": evidence_uris,
        "adapter_provenance": provenance,
        "synthetic": synthetic,
    }
    if evidence_id is not None:
        evidence_values["evidence_id"] = evidence_id
    evidence = EvidenceBundle.model_validate(evidence_values)
    report = _render_report(evaluation, comparison)
    return SmokeEvidenceArtifacts(
        evaluation=evaluation,
        evidence=evidence,
        report_markdown=report,
    )


def write_evidence_artifacts(
    root: Path,
    comparison: EquivalenceResult,
    artifacts: SmokeEvidenceArtifacts,
) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    aggregate_names = (
        "comparison.json",
        "evaluation-run.json",
        "evidence-bundle.json",
        "report.md",
        "sha256sums.json",
    )
    existing = [name for name in aggregate_names if (root / name).exists()]
    if existing:
        raise ValueError(
            "refusing to overwrite aggregate evidence: " + ", ".join(existing)
        )
    observed: dict[str, tuple[bool, bool]] = {}
    for variant in ("baseline", "noop"):
        variant_dir = root / variant
        missing = sorted(
            name for name in VARIANT_EVIDENCE_FILES if not (variant_dir / name).is_file()
        )
        if missing:
            raise ValueError(
                f"{variant} evidence is incomplete; missing: {', '.join(missing)}"
            )
        symlinks = sorted(
            name for name in VARIANT_EVIDENCE_FILES if (variant_dir / name).is_symlink()
        )
        if variant_dir.is_symlink() or symlinks:
            raise ValueError(f"{variant} evidence cannot contain symbolic links")
        expected_output = getattr(comparison, variant)
        observed[variant] = _validate_variant_result(variant_dir, expected_output)

    metrics = artifacts.evaluation.metrics
    for variant in ("baseline", "noop"):
        execution_succeeded, cleanup_succeeded = observed[variant]
        if metrics.get(f"{variant}_execution_succeeded") is not execution_succeeded:
            raise ValueError(f"{variant} result status does not match EvaluationRun")
        if artifacts.evaluation.passed and not cleanup_succeeded:
            raise ValueError(f"passing evidence requires successful {variant} cleanup")
    if metrics.get("output_equivalent") is not comparison.passed:
        raise ValueError("comparison verdict does not match EvaluationRun")
    cleanup_healthy = metrics.get("cleanup_healthy")
    if not isinstance(cleanup_healthy, bool):
        raise ValueError("EvaluationRun lacks a boolean cleanup health verdict")
    expected_passed = all(
        (
            comparison.passed,
            observed["baseline"][0],
            observed["noop"][0],
            cleanup_healthy,
        )
    )
    if artifacts.evaluation.passed is not expected_passed:
        raise ValueError("aggregate evidence verdict does not match variant evidence")
    _atomic_write_json(root / "comparison.json", comparison.model_dump(mode="json"))
    _atomic_write_json(
        root / "evaluation-run.json", artifacts.evaluation.model_dump(mode="json")
    )
    _atomic_write_json(
        root / "evidence-bundle.json", artifacts.evidence.model_dump(mode="json")
    )
    _atomic_write_text(root / "report.md", artifacts.report_markdown)
    manifest = _hash_evidence_tree(root)
    _atomic_write_json(root / "sha256sums.json", manifest)
    return manifest


def _validate_variant_result(
    variant_dir: Path,
    expected_output: NormalizedSGLangOutput | None,
) -> tuple[bool, bool]:
    result = _read_json_object(variant_dir / "result.json")
    stop = _read_json_object(variant_dir / "stop.json")
    response = _read_json_object(variant_dir / "response.json")
    status = result.get("status")
    if status not in {"succeeded", "failed"}:
        raise ValueError(f"{variant_dir.name} result has an invalid status")
    cleanup_succeeded = stop.get("cleanup_succeeded")
    if not isinstance(cleanup_succeeded, bool):
        raise ValueError(f"{variant_dir.name} stop evidence lacks a boolean cleanup result")
    if result.get("cleanup_succeeded") is not cleanup_succeeded:
        raise ValueError(f"{variant_dir.name} result and stop cleanup disagree")

    normalized_raw = result.get("normalized_output")
    normalized = (
        NormalizedSGLangOutput.model_validate(normalized_raw)
        if normalized_raw is not None
        else None
    )
    if normalized != expected_output:
        raise ValueError(f"{variant_dir.name} normalized output does not match comparison")
    if normalized is not None:
        try:
            response_output = normalize_response(response.get("body_json"))
        except SGLangResponseError as exc:
            raise ValueError(
                f"{variant_dir.name} response does not support its normalized output"
            ) from exc
        if response_output != normalized:
            raise ValueError(f"{variant_dir.name} result and raw response disagree")
    if status == "succeeded" and (normalized is None or not cleanup_succeeded):
        raise ValueError(
            f"{variant_dir.name} succeeded without normalized output and cleanup"
        )
    return status == "succeeded", cleanup_succeeded


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON evidence file {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"evidence file {path.name} must contain a JSON object")
    return value


def _render_report(
    evaluation: EvaluationRun,
    comparison: EquivalenceResult,
) -> str:
    status = "通过" if evaluation.passed else "失败"
    differing = "、".join(sorted(comparison.differences)) or "无"
    errors = "；".join(
        f"{variant}: {message}" for variant, message in sorted(comparison.errors.items())
    ) or "无"
    baseline_hash = comparison.baseline_hash or "unavailable"
    noop_hash = comparison.noop_hash or "unavailable"
    return "\n".join(
        (
            "# SGLang Framework Smoke 报告",
            "",
            f"- 判定：{status}",
            f"- 协议：{evaluation.protocol_version}",
            f"- Evaluation Run：`{evaluation.evaluation_run_id}`",
            f"- Baseline 输出哈希：`{baseline_hash}`",
            f"- No-op 输出哈希：`{noop_hash}`",
            f"- 差异字段：{differing}",
            f"- 执行或解析错误：{errors}",
            "",
            NO_PERFORMANCE_CONCLUSION,
            "",
        )
    )


def _validate_absolute_posix_path(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or path == PurePosixPath("/")
        or ".." in path.parts
        or "\x00" in value
    ):
        raise ValueError(f"{label} must be a clean absolute POSIX path")
    return value


def _target_fingerprint(target: TargetSpec) -> str:
    encoded = json.dumps(
        target.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_text(
        path,
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def _hash_evidence_tree(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "sha256sums.json" or path.name.endswith(".tmp"):
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        hashes[path.relative_to(root).as_posix()] = f"sha256:{digest.hexdigest()}"
    return hashes


def verify_evidence_manifest(root: Path) -> None:
    manifest_path = root / "sha256sums.json"
    if manifest_path.is_symlink():
        raise ValueError("sha256sums.json cannot be a symbolic link")
    manifest = _read_json_object(manifest_path)
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and path.name != "sha256sums.json"
        and not path.name.endswith(".tmp")
    }
    if set(manifest) != actual_paths:
        raise ValueError("SHA-256 manifest file set does not match evidence tree")
    for relative, expected in manifest.items():
        if not isinstance(expected, str) or not expected.startswith("sha256:"):
            raise ValueError(f"invalid SHA-256 manifest entry: {relative}")
        path = root / PurePosixPath(relative)
        if path.is_symlink():
            raise ValueError(f"evidence manifest cannot reference a symlink: {relative}")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if expected != f"sha256:{digest.hexdigest()}":
            raise ValueError(f"SHA-256 mismatch for evidence file: {relative}")
