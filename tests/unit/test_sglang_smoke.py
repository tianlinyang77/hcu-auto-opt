from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from hcuopt.contracts.platform_v1 import AdapterProvenance, ArtifactManifest, MountSpec
from hcuopt.domain.enums import LeaseScope
from hcuopt.evaluation.sglang_smoke import (
    FRAMEWORK_SMOKE_PROTOCOL_VERSION,
    NO_PERFORMANCE_CONCLUSION,
    VARIANT_EVIDENCE_FILES,
    SGLangResponseError,
    SGLangWorkloadSpec,
    SmokeVariantSpec,
    build_evidence,
    build_execution_request,
    build_failed_comparison,
    compare_outputs,
    load_workload_spec,
    normalize_response,
    validate_variant_pair,
    verify_evidence_manifest,
    write_evidence_artifacts,
)
from hcuopt.targets import load_target

ROOT = Path(__file__).parents[2]
TARGET_PATH = ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml"
WORKLOAD_PATH = ROOT / "config" / "workloads" / "nmz36-sglang-smoke-v1.yaml"


def _valid_response(text: str = " Paris") -> dict[str, object]:
    return {
        "text": text,
        "output_ids": [1, 2],
        "meta_info": {
            "id": "dynamic-id",
            "finish_reason": {"type": "stop", "matched": None},
            "prompt_tokens": 5,
            "completion_tokens": 2,
            "e2e_latency": 12.34,
            "cached_tokens": 0,
        },
    }


def _provenance(capability: str) -> AdapterProvenance:
    return AdapterProvenance(
        profile="scripted-framework-smoke",
        capability=capability,
        adapter_name=f"Scripted{capability.title()}",
        adapter_version="1",
        implementation_kind="fake",
    )


def _target_fingerprint() -> str:
    target = load_target(TARGET_PATH)
    encoded = json.dumps(
        target.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _write_variant_evidence(
    directory: Path,
    *,
    response: dict[str, object] | None,
    status: str,
    cleanup_succeeded: bool = True,
) -> None:
    directory.mkdir()
    normalized = normalize_response(response).model_dump(mode="json") if response else None
    structured: dict[str, object] = {
        "spec.json": {"protocol_version": "sglang-smoke-v1"},
        "environment.json": {"allowlisted_environment": {}},
        "start.json": {"status": "started", "pid": 123},
        "request.json": {"attempted": True},
        "response.json": {
            "attempted": True,
            "http_status": 200 if response else 500,
            "body_json": response,
            "body_text": json.dumps(response) if response else "scripted failure",
            "parse_error": None if response else "scripted failure",
            "truncated": False,
        },
        "stop.json": {"cleanup_succeeded": cleanup_succeeded},
        "result.json": {
            "status": status,
            "normalized_output": normalized,
            "cleanup_succeeded": cleanup_succeeded,
        },
    }
    for name, value in structured.items():
        (directory / name).write_text(json.dumps(value) + "\n", encoding="utf-8")
    (directory / "ready.jsonl").write_text('{"outcome":"ready"}\n', encoding="utf-8")
    (directory / "server.log").write_text("scripted server\n", encoding="utf-8")
    assert {path.name for path in directory.iterdir()} == VARIANT_EVIDENCE_FILES


def test_locked_workload_has_the_exact_deterministic_request() -> None:
    workload = load_workload_spec(WORKLOAD_PATH)
    assert workload.target_id == "nmz36-sglang-0.5.12"
    assert workload.request_payload() == {
        "text": "The capital of France is",
        "sampling_params": {
            "temperature": 0.0,
            "max_new_tokens": 8,
            "sampling_seed": 0,
        },
        "stream": False,
    }
    assert "seed" not in workload.request_payload()["sampling_params"]
    assert workload.server_argv() == [
        "sglang",
        "serve",
        "--model-path",
        "/public/opendas/DL_DATA/llm-models/qwen2.5/Qwen2.5-0.5B-Instruct",
        "--served-model-name",
        "hcuopt-smoke",
        "--host",
        "127.0.0.1",
        "--port",
        "30000",
        "--tp-size",
        "1",
        "--trust-remote-code",
        "--attention-backend",
        "fa3",
        "--page-size",
        "64",
        "--mem-fraction-static",
        "0.85",
    ]
    assert workload.cookbook_commit == "2a7f431301e41e6ea1f377129bf5ba3e43ae299f"


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ({"unexpected": True}, "Extra inputs"),
        ({"port": "30000"}, "valid integer"),
        ({"temperature": 0.1}, "temperature=0.0"),
        ({"stream": True}, "stream=false"),
        ({"model_path": "relative/model"}, "absolute"),
        ({"execution_timeout_seconds": 370}, "must exceed"),
        ({"server_entrypoint": "python"}, "sglang"),
        ({"attention_backend": "torch_native"}, "fa3"),
        ({"page_size": 32}, "64"),
        ({"mem_fraction_static": 0.9}, "0.85"),
        ({"cookbook_paths": ["../other.md"]}, "clean relative"),
    ),
)
def test_workload_rejects_unsafe_or_nondeterministic_values(
    mutation: dict[str, object], message: str
) -> None:
    raw = yaml.safe_load(WORKLOAD_PATH.read_text(encoding="utf-8"))
    raw.update(mutation)
    with pytest.raises(ValidationError, match=message):
        SGLangWorkloadSpec.model_validate(raw)


def test_variant_requires_exactly_the_noop_artifact_mount() -> None:
    artifact_id = uuid4()
    with pytest.raises(ValidationError, match="requires an artifact_id"):
        SmokeVariantSpec(
            name="noop",
            runner_host_path="/home/github/runner.py",
            spec_host_path="/home/github/spec.json",
            evidence_host_dir="/home/github/results/noop",
        )
    with pytest.raises(ValidationError, match="exactly one artifact mount"):
        SmokeVariantSpec(
            name="noop",
            artifact_id=artifact_id,
            runner_host_path="/home/github/runner.py",
            spec_host_path="/home/github/spec.json",
            evidence_host_dir="/home/github/results/noop",
        )
    with pytest.raises(ValidationError, match="baseline variant cannot mount"):
        SmokeVariantSpec(
            name="baseline",
            runner_host_path="/home/github/runner.py",
            spec_host_path="/home/github/spec.json",
            evidence_host_dir="/home/github/results/baseline",
            mounts=[MountSpec(source="/home/github/a.so", target="/opt/a.so")],
        )


def test_execution_request_is_digest_locked_leased_and_self_contained() -> None:
    target = load_target(TARGET_PATH)
    workload = load_workload_spec(WORKLOAD_PATH)
    variant = SmokeVariantSpec(
        name="baseline",
        runner_host_path="/home/github/hcu-auto-opt/sglang_smoke_runner.py",
        spec_host_path="/home/github/hcu-auto-opt/results/input/spec.json",
        evidence_host_dir="/home/github/hcu-auto-opt/results/run/baseline",
    )
    request = build_execution_request(
        target,
        workload,
        variant,
        resource_id="hcu-7",
        fencing_token=9,
    )
    assert request.container_image == target.inference_image.immutable_reference
    assert request.lease_scope is LeaseScope.EXCLUSIVE
    assert request.resource_id == "hcu-7"
    assert request.fencing_token == 9
    assert request.timeout_seconds == 420
    assert request.argv == [
        "python",
        "/opt/hcuopt/sglang_smoke_runner.py",
        "--spec",
        "/work/input/spec.json",
        "--evidence-dir",
        "/work/output",
    ]
    assert {mount.target for mount in request.mounts} == {
        "/opt/hcuopt/sglang_smoke_runner.py",
        "/opt/hyhal",
        "/work/input/spec.json",
        "/work/output",
        workload.model_path,
    }
    runtime_mount = next(mount for mount in request.mounts if mount.target == "/opt/hyhal")
    assert runtime_mount.source == "/opt/hyhal"
    assert runtime_mount.read_only is True


def test_noop_request_is_bound_to_the_declared_artifact() -> None:
    target = load_target(TARGET_PATH)
    workload = load_workload_spec(WORKLOAD_PATH)
    candidate_id = uuid4()
    artifact = ArtifactManifest(
        candidate_id=candidate_id,
        kind="noop_shared_object",
        uri="file:///home/github/artifacts/noop.so",
        content_hash="sha256:" + "e" * 64,
    )
    baseline = SmokeVariantSpec(
        name="baseline",
        runner_host_path="/home/github/hcu-auto-opt/sglang_smoke_runner.py",
        spec_host_path="/home/github/hcu-auto-opt/results/input/spec.json",
        evidence_host_dir="/home/github/hcu-auto-opt/results/run/baseline",
    )
    noop = SmokeVariantSpec(
        name="noop",
        artifact_id=artifact.artifact_id,
        runner_host_path=baseline.runner_host_path,
        spec_host_path=baseline.spec_host_path,
        evidence_host_dir="/home/github/hcu-auto-opt/results/run/noop",
        mounts=[
            MountSpec(
                source="/home/github/artifacts/noop.so",
                target="/opt/hcuopt/artifacts/noop.so",
                read_only=True,
            )
        ],
    )
    validate_variant_pair(baseline, noop, artifact)
    request = build_execution_request(target, workload, noop, artifact)
    artifact_mount = request.mounts[-1]
    assert artifact_mount.source == "/home/github/artifacts/noop.so"
    assert artifact_mount.target == "/opt/hcuopt/artifacts/noop.so"
    assert artifact_mount.read_only is True

    wrong_artifact = artifact.model_copy(update={"artifact_id": uuid4()})
    with pytest.raises(ValueError, match="matching ArtifactManifest"):
        build_execution_request(target, workload, noop, wrong_artifact)


def test_execution_request_rejects_bad_binding_and_mounts() -> None:
    target = load_target(TARGET_PATH)
    workload = load_workload_spec(WORKLOAD_PATH)
    baseline = SmokeVariantSpec(
        name="baseline",
        runner_host_path="/home/github/runner.py",
        spec_host_path="/home/github/spec.json",
        evidence_host_dir="/home/github/results/baseline",
    )
    with pytest.raises(ValueError, match="supplied together"):
        build_execution_request(target, workload, baseline, resource_id="hcu-7")

    escaped = baseline.model_copy(update={"evidence_host_dir": "/tmp/evidence"})
    with pytest.raises(ValueError, match="must stay under"):
        build_execution_request(target, workload, escaped)

    traversing = baseline.model_copy(
        update={"evidence_host_dir": "/home/github/../data/evidence"}
    )
    with pytest.raises(ValueError, match="clean absolute"):
        build_execution_request(target, workload, traversing)

    overlap_artifact = ArtifactManifest(
        kind="noop_shared_object",
        uri="file:///home/github/noop.so",
        content_hash="sha256:" + "f" * 64,
    )
    overlapping = SmokeVariantSpec(
        name="noop",
        artifact_id=overlap_artifact.artifact_id,
        runner_host_path="/home/github/runner.py",
        spec_host_path="/home/github/spec.json",
        evidence_host_dir="/home/github/results/noop",
        mounts=[MountSpec(source="/home/github/noop.so", target="/work")],
    )
    with pytest.raises(ValueError, match="overlapping mount targets"):
        build_execution_request(target, workload, overlapping, overlap_artifact)


def test_normalize_accepts_real_shape_and_ignores_dynamic_fields() -> None:
    first = normalize_response(_valid_response())
    changed = _valid_response()
    changed["output_ids"] = [99]
    assert isinstance(changed["meta_info"], dict)
    changed["meta_info"]["id"] = "another-id"
    changed["meta_info"]["e2e_latency"] = 999
    second = normalize_response(json.dumps(changed).encode())
    assert first == second
    assert first.text == " Paris"


@pytest.mark.parametrize(
    "response",
    (
        b"\xff",
        "not-json",
        [],
        {},
        {"text": "x", "meta_info": None},
        {"text": "x", "meta_info": {"finish_reason": None}},
        {
            "text": "x",
            "meta_info": {
                "finish_reason": {"type": "stop"},
                "prompt_tokens": True,
                "completion_tokens": 1,
            },
        },
        {
            "text": "x",
            "meta_info": {
                "finish_reason": {"type": "stop"},
                "prompt_tokens": 1,
                "completion_tokens": -1,
            },
        },
    ),
)
def test_normalize_rejects_malformed_responses(response: object) -> None:
    with pytest.raises(SGLangResponseError):
        normalize_response(response)  # type: ignore[arg-type]


def test_comparison_is_strict_and_produces_structured_differences() -> None:
    baseline = normalize_response(_valid_response(" Paris"))
    noop = normalize_response(_valid_response(" Paris "))
    result = compare_outputs(baseline, noop)
    assert result.passed is False
    assert set(result.differences) == {"text"}
    assert result.differences["text"].baseline == " Paris"
    assert result.differences["text"].noop == " Paris "
    assert result.baseline_hash != result.noop_hash


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("finish_reason_type", "length"),
        ("prompt_tokens", 6),
        ("completion_tokens", 3),
    ),
)
def test_each_normalized_field_is_an_independent_gate(field: str, value: object) -> None:
    baseline = normalize_response(_valid_response())
    noop = baseline.model_copy(update={field: value})
    comparison = compare_outputs(baseline, noop)
    assert comparison.passed is False
    assert set(comparison.differences) == {field}


def test_evidence_binds_identity_and_contains_no_performance_claims() -> None:
    target = load_target(TARGET_PATH)
    candidate_id = uuid4()
    task_id = uuid4()
    baseline_epoch_id = uuid4()
    artifact = ArtifactManifest(
        candidate_id=candidate_id,
        kind="scripted_noop",
        uri="fake://artifacts/noop.so",
        content_hash="sha256:" + "a" * 64,
        synthetic=True,
    )
    output = normalize_response(_valid_response())
    attempt_ids = [uuid4(), uuid4()]
    artifacts = build_evidence(
        task_id=task_id,
        candidate_id=candidate_id,
        round_id=uuid4(),
        baseline_epoch_id=baseline_epoch_id,
        target=target,
        target_fingerprint=_target_fingerprint(),
        artifact=artifact,
        comparison=compare_outputs(output, output),
        adapter_provenance=[
            _provenance("executor"),
            _provenance("evaluator"),
            _provenance("resource_cleaner"),
        ],
        idempotency_key="scripted-framework-smoke-evaluation",
        baseline_execution_attempt_id=attempt_ids[0],
        noop_execution_attempt_id=attempt_ids[1],
        evidence_root_uri="file:///framework-smoke/attempt-0001",
        sha256_manifest_uri="file:///framework-smoke/attempt-0001/sha256sums.json",
        raw_uris=["file:///baseline/result.json", "file:///noop/result.json"],
        baseline_execution_succeeded=True,
        noop_execution_succeeded=True,
        cleanup_healthy=True,
    )
    assert artifacts.evaluation.phase == "correctness"
    assert artifacts.evaluation.protocol_version == FRAMEWORK_SMOKE_PROTOCOL_VERSION
    assert artifacts.evaluation.measurement is None
    assert artifacts.evaluation.passed is True
    assert artifacts.evaluation.synthetic is True
    assert artifacts.evidence.artifact_ids == [artifact.artifact_id]
    assert artifacts.evidence.task_id == task_id
    assert artifacts.evidence.candidate_id == candidate_id
    assert artifacts.evidence.baseline_epoch_id == baseline_epoch_id
    assert artifacts.evidence.target_id == target.target_id
    assert artifacts.evidence.summary["execution_attempt_ids"] == {
        "baseline": str(attempt_ids[0]),
        "noop": str(attempt_ids[1]),
    }
    assert artifacts.evidence.measurement_ids == []
    assert NO_PERFORMANCE_CONCLUSION in artifacts.report_markdown
    serialized = json.dumps(artifacts.model_dump(mode="json"))
    for forbidden in ("speedup_ratio", "latency", "throughput", "ci_low", "ci_high"):
        assert forbidden not in serialized


def test_evidence_rejects_wrong_artifact_or_incomplete_provenance() -> None:
    target = load_target(TARGET_PATH)
    output = normalize_response(_valid_response())
    common = {
        "task_id": uuid4(),
        "candidate_id": uuid4(),
        "round_id": uuid4(),
        "baseline_epoch_id": uuid4(),
        "target": target,
        "target_fingerprint": _target_fingerprint(),
        "comparison": compare_outputs(output, output),
        "idempotency_key": "scripted-framework-smoke-evaluation",
        "baseline_execution_attempt_id": uuid4(),
        "noop_execution_attempt_id": uuid4(),
        "evidence_root_uri": "file:///framework-smoke/attempt-0001",
        "sha256_manifest_uri": (
            "file:///framework-smoke/attempt-0001/sha256sums.json"
        ),
        "raw_uris": ["file:///baseline/result.json", "file:///noop/result.json"],
        "baseline_execution_succeeded": True,
        "noop_execution_succeeded": True,
        "cleanup_healthy": True,
    }
    artifact = ArtifactManifest(
        candidate_id=uuid4(),
        kind="scripted_noop",
        uri="fake://artifacts/noop.so",
        content_hash="sha256:" + "b" * 64,
        synthetic=True,
    )
    with pytest.raises(ValueError, match="candidate_id"):
        build_evidence(
            **common,
            artifact=artifact,
            adapter_provenance=[_provenance("executor"), _provenance("evaluator")],
        )

    artifact = artifact.model_copy(update={"candidate_id": common["candidate_id"]})
    with pytest.raises(ValueError, match="evaluator"):
        build_evidence(
            **common,
            artifact=artifact,
            adapter_provenance=[_provenance("executor")],
        )

    wrong_target = dict(common)
    wrong_target["target_fingerprint"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="does not match TargetSpec"):
        build_evidence(
            **wrong_target,
            artifact=artifact,
            adapter_provenance=[_provenance("executor"), _provenance("evaluator")],
        )

    repeated_attempt = dict(common)
    repeated_attempt["noop_execution_attempt_id"] = repeated_attempt[
        "baseline_execution_attempt_id"
    ]
    with pytest.raises(ValueError, match="distinct execution attempt IDs"):
        build_evidence(
            **repeated_attempt,
            artifact=artifact,
            adapter_provenance=[_provenance("executor"), _provenance("evaluator")],
        )


def test_evidence_files_are_complete_atomic_and_hash_verifiable(tmp_path: Path) -> None:
    target = load_target(TARGET_PATH)
    candidate_id = uuid4()
    output = normalize_response(_valid_response())
    comparison = compare_outputs(output, output)
    artifact = ArtifactManifest(
        candidate_id=candidate_id,
        kind="scripted_noop",
        uri="fake://artifacts/noop.so",
        content_hash="sha256:" + "c" * 64,
        synthetic=True,
    )
    artifacts = build_evidence(
        task_id=uuid4(),
        candidate_id=candidate_id,
        round_id=uuid4(),
        baseline_epoch_id=uuid4(),
        target=target,
        target_fingerprint=_target_fingerprint(),
        artifact=artifact,
        comparison=comparison,
        adapter_provenance=[_provenance("executor"), _provenance("evaluator")],
        idempotency_key="scripted-framework-smoke-evaluation",
        baseline_execution_attempt_id=uuid4(),
        noop_execution_attempt_id=uuid4(),
        evidence_root_uri="file:///framework-smoke/attempt-0001",
        sha256_manifest_uri="file:///framework-smoke/attempt-0001/sha256sums.json",
        raw_uris=["file:///baseline/result.json", "file:///noop/result.json"],
        baseline_execution_succeeded=True,
        noop_execution_succeeded=True,
        cleanup_healthy=True,
    )
    _write_variant_evidence(
        tmp_path / "baseline",
        response=_valid_response(),
        status="succeeded",
    )
    _write_variant_evidence(
        tmp_path / "noop",
        response=_valid_response(),
        status="succeeded",
    )

    manifest = write_evidence_artifacts(tmp_path, comparison, artifacts)
    assert "sha256sums.json" not in manifest
    assert "baseline/spec.json" in manifest
    assert "noop/result.json" in manifest
    assert "comparison.json" in manifest
    for relative, expected in manifest.items():
        digest = hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest()
        assert expected == f"sha256:{digest}"
    assert not list(tmp_path.rglob("*.tmp"))
    verify_evidence_manifest(tmp_path)
    (tmp_path / "baseline" / "server.log").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        verify_evidence_manifest(tmp_path)
    with pytest.raises(ValueError, match="refusing to overwrite aggregate evidence"):
        write_evidence_artifacts(tmp_path, comparison, artifacts)


def test_failed_noop_still_produces_complete_aggregate_evidence(tmp_path: Path) -> None:
    target = load_target(TARGET_PATH)
    candidate_id = uuid4()
    baseline_output = normalize_response(_valid_response())
    comparison = build_failed_comparison(
        baseline=baseline_output,
        noop_error="generate response is not valid JSON",
    )
    artifact = ArtifactManifest(
        candidate_id=candidate_id,
        kind="scripted_noop",
        uri="fake://artifacts/noop.so",
        content_hash="sha256:" + "9" * 64,
        synthetic=True,
    )
    artifacts = build_evidence(
        task_id=uuid4(),
        candidate_id=candidate_id,
        round_id=uuid4(),
        baseline_epoch_id=uuid4(),
        target=target,
        target_fingerprint=_target_fingerprint(),
        artifact=artifact,
        comparison=comparison,
        adapter_provenance=[_provenance("executor"), _provenance("evaluator")],
        idempotency_key="scripted-framework-smoke-failed-noop",
        baseline_execution_attempt_id=uuid4(),
        noop_execution_attempt_id=uuid4(),
        evidence_root_uri="file:///framework-smoke/attempt-0001",
        sha256_manifest_uri="file:///framework-smoke/attempt-0001/sha256sums.json",
        raw_uris=["file:///baseline/result.json", "file:///noop/result.json"],
        baseline_execution_succeeded=True,
        noop_execution_succeeded=False,
        cleanup_healthy=True,
    )
    _write_variant_evidence(
        tmp_path / "baseline",
        response=_valid_response(),
        status="succeeded",
    )
    _write_variant_evidence(
        tmp_path / "noop",
        response=None,
        status="failed",
    )
    manifest = write_evidence_artifacts(tmp_path, comparison, artifacts)
    assert artifacts.evaluation.passed is False
    assert comparison.errors == {"noop": "generate response is not valid JSON"}
    for name in (
        "comparison.json",
        "evaluation-run.json",
        "evidence-bundle.json",
        "report.md",
        "sha256sums.json",
    ):
        assert (tmp_path / name).is_file()
    assert "noop/result.json" in manifest


def test_final_evidence_refuses_missing_variant_files(tmp_path: Path) -> None:
    target = load_target(TARGET_PATH)
    candidate_id = uuid4()
    output = normalize_response(_valid_response())
    artifact = ArtifactManifest(
        candidate_id=candidate_id,
        kind="scripted_noop",
        uri="fake://artifacts/noop.so",
        content_hash="sha256:" + "d" * 64,
        synthetic=True,
    )
    artifacts = build_evidence(
        task_id=uuid4(),
        candidate_id=candidate_id,
        round_id=uuid4(),
        baseline_epoch_id=uuid4(),
        target=target,
        target_fingerprint=_target_fingerprint(),
        artifact=artifact,
        comparison=compare_outputs(output, output),
        adapter_provenance=[_provenance("executor"), _provenance("evaluator")],
        idempotency_key="scripted-framework-smoke-evaluation",
        baseline_execution_attempt_id=uuid4(),
        noop_execution_attempt_id=uuid4(),
        evidence_root_uri="file:///framework-smoke/attempt-0001",
        sha256_manifest_uri="file:///framework-smoke/attempt-0001/sha256sums.json",
        raw_uris=["file:///baseline/result.json", "file:///noop/result.json"],
        baseline_execution_succeeded=True,
        noop_execution_succeeded=True,
        cleanup_healthy=True,
    )
    with pytest.raises(ValueError, match="baseline evidence is incomplete"):
        write_evidence_artifacts(tmp_path, compare_outputs(output, output), artifacts)
