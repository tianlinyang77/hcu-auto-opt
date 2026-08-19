from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from hcuopt.adapters.profiles import REAL_FRAMEWORK_SMOKE_PROFILE, AdapterProfileCatalog
from hcuopt.adapters.real_profile import build_nmz36_framework_smoke_registry
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.adapters.sglang_evaluator import SGLangSmokeEvaluator, SGLangSmokePlan
from hcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    ArtifactManifest,
    EvaluationRun,
    EvidenceBundle,
    ExecutionAttempt,
    ExecutionRequest,
    ExecutionResult,
)
from hcuopt.contracts.v1 import PairedFrameworkSmokeResult
from hcuopt.domain.enums import LeaseScope
from hcuopt.domain.errors import ExecutionSafetyError
from hcuopt.evaluation.sglang_smoke import VARIANT_EVIDENCE_FILES, normalize_response
from hcuopt.targets import load_target
from hcuopt.workers.handlers import JobHandlers

ROOT = Path(__file__).parents[2]
TARGET = load_target(ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml")
WORKLOAD = ROOT / "config" / "workloads" / "nmz36-sglang-smoke-v1.yaml"


def _provenance(capability: str) -> AdapterProvenance:
    return AdapterProvenance(
        profile=REAL_FRAMEWORK_SMOKE_PROFILE,
        capability=capability,
        adapter_name=f"Fixture{capability.title()}",
        adapter_version="1",
        implementation_kind="real",
    )


def _target_fingerprint() -> str:
    encoded = json.dumps(
        TARGET.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _response() -> dict[str, object]:
    return {
        "text": " Paris",
        "meta_info": {
            "finish_reason": {"type": "stop"},
            "prompt_tokens": 5,
            "completion_tokens": 2,
        },
    }


def _write_variant(directory: Path) -> None:
    output = normalize_response(_response()).model_dump(mode="json")
    values: dict[str, object] = {
        "spec.json": {"protocol_version": "sglang-smoke-v1"},
        "environment.json": {"allowlisted_environment": {}},
        "start.json": {"status": "started", "pid": 1},
        "request.json": {"attempted": True},
        "response.json": {"body_json": _response()},
        "stop.json": {"cleanup_succeeded": True},
        "result.json": {
            "status": "succeeded",
            "normalized_output": output,
            "cleanup_succeeded": True,
        },
    }
    for name, value in values.items():
        (directory / name).write_text(json.dumps(value) + "\n", encoding="utf-8")
    (directory / "ready.jsonl").write_text('{"outcome":"ready"}\n', encoding="utf-8")
    (directory / "server.log").write_text("fixture\n", encoding="utf-8")
    assert {item.name for item in directory.iterdir()} == VARIANT_EVIDENCE_FILES


def _execution(request: ExecutionRequest) -> ExecutionResult:
    now = datetime.now(timezone.utc)
    return ExecutionResult(
        request_id=request.request_id,
        status="succeeded",
        exit_code=0,
        started_at=now,
        finished_at=now,
        adapter_provenance=_provenance("executor"),
        synthetic=False,
    )


def test_default_catalog_and_runtime_registry_expose_the_same_real_profile(
    tmp_path: Path,
) -> None:
    target = TARGET.model_copy(
        update={
            "blockers": [
                blocker.model_copy(update={"status": "resolved"})
                if blocker.id == "locked_image_dependency_conflict"
                else blocker
                for blocker in TARGET.blockers
            ]
        }
    )
    profile = AdapterProfileCatalog().require(REAL_FRAMEWORK_SMOKE_PROFILE)
    profile.validate_target(target)
    registry = build_nmz36_framework_smoke_registry(target, tmp_path)

    assert registry.profile == REAL_FRAMEWORK_SMOKE_PROFILE
    assert set(registry.available()) == set(profile.capabilities)
    for name in registry.available():
        provenance = registry.require(name).provenance
        assert provenance.profile == REAL_FRAMEWORK_SMOKE_PROFILE
        assert provenance.implementation_kind == "real"


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX host paths")
def test_real_evaluator_builds_two_distinct_container_requests(tmp_path: Path) -> None:
    runner_path = tmp_path / "sglang_smoke_runner.py"
    runner_path.write_text("# fixture\n", encoding="utf-8")
    artifact_path = tmp_path / "noop.tar"
    artifact_path.write_bytes(b"noop")
    artifact = ArtifactManifest(
        candidate_id=uuid4(),
        kind="noop-source-archive",
        uri=artifact_path.as_uri(),
        content_hash="sha256:" + hashlib.sha256(b"noop").hexdigest(),
    )
    host = TARGET.execution_host.model_copy(update={"work_root": tmp_path.as_posix()})
    target = TARGET.model_copy(update={"execution_host": host})
    evaluator = SGLangSmokeEvaluator(
        workload_path=WORKLOAD,
        runner_host_path=runner_path,
    )
    plan = evaluator.prepare_framework_smoke(
        target=target,
        artifact=artifact,
        output_dir=tmp_path / "results",
        task_id=uuid4(),
        evaluation_run_id=uuid4(),
        attempt_number=1,
        baseline_request_id=uuid4(),
        noop_request_id=uuid4(),
        resource_id="hcu-7",
        fencing_token=1,
    )

    assert plan.baseline_request.request_id != plan.noop_request.request_id
    assert len(plan.baseline_request.mounts) + 1 == len(plan.noop_request.mounts)
    assert plan.noop_request.mounts[-1].source == artifact_path.as_posix()
    assert plan.baseline_evidence_dir != plan.noop_evidence_dir


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX host paths")
def test_real_evaluator_rejects_tampered_noop_artifact(tmp_path: Path) -> None:
    runner_path = tmp_path / "sglang_smoke_runner.py"
    runner_path.write_text("# fixture\n", encoding="utf-8")
    artifact_path = tmp_path / "noop.tar"
    artifact_path.write_bytes(b"tampered")
    artifact = ArtifactManifest(
        candidate_id=uuid4(),
        kind="noop-source-archive",
        uri=artifact_path.as_uri(),
        content_hash="sha256:" + hashlib.sha256(b"original").hexdigest(),
    )
    host = TARGET.execution_host.model_copy(update={"work_root": tmp_path.as_posix()})
    target = TARGET.model_copy(update={"execution_host": host})
    evaluator = SGLangSmokeEvaluator(
        workload_path=WORKLOAD,
        runner_host_path=runner_path,
    )

    with pytest.raises(ValueError, match="content hash does not match"):
        evaluator.prepare_framework_smoke(
            target=target,
            artifact=artifact,
            output_dir=tmp_path / "results",
            task_id=uuid4(),
            evaluation_run_id=uuid4(),
            attempt_number=1,
            baseline_request_id=uuid4(),
            noop_request_id=uuid4(),
            resource_id="hcu-7",
            fencing_token=1,
        )


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symbolic links")
def test_real_evaluator_rejects_symlinked_noop_artifact(tmp_path: Path) -> None:
    runner_path = tmp_path / "sglang_smoke_runner.py"
    runner_path.write_text("# fixture\n", encoding="utf-8")
    target_path = tmp_path / "published-noop.tar"
    target_path.write_bytes(b"noop")
    artifact_path = tmp_path / "noop.tar"
    artifact_path.symlink_to(target_path)
    artifact = ArtifactManifest(
        candidate_id=uuid4(),
        kind="noop-source-archive",
        uri=artifact_path.as_uri(),
        content_hash="sha256:" + hashlib.sha256(b"noop").hexdigest(),
    )
    host = TARGET.execution_host.model_copy(update={"work_root": tmp_path.as_posix()})
    target = TARGET.model_copy(update={"execution_host": host})
    evaluator = SGLangSmokeEvaluator(
        workload_path=WORKLOAD,
        runner_host_path=runner_path,
    )

    with pytest.raises(ValueError, match="cannot be a symbolic link"):
        evaluator.prepare_framework_smoke(
            target=target,
            artifact=artifact,
            output_dir=tmp_path / "results",
            task_id=uuid4(),
            evaluation_run_id=uuid4(),
            attempt_number=1,
            baseline_request_id=uuid4(),
            noop_request_id=uuid4(),
            resource_id="hcu-7",
            fencing_token=1,
        )


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX host paths")
def test_real_evaluator_rejects_wrong_artifact_kind(tmp_path: Path) -> None:
    runner_path = tmp_path / "sglang_smoke_runner.py"
    runner_path.write_text("# fixture\n", encoding="utf-8")
    artifact_path = tmp_path / "noop.tar"
    artifact_path.write_bytes(b"noop")
    artifact = ArtifactManifest(
        candidate_id=uuid4(),
        kind="shared-library",
        uri=artifact_path.as_uri(),
        content_hash="sha256:" + hashlib.sha256(b"noop").hexdigest(),
    )
    host = TARGET.execution_host.model_copy(update={"work_root": tmp_path.as_posix()})
    target = TARGET.model_copy(update={"execution_host": host})
    evaluator = SGLangSmokeEvaluator(
        workload_path=WORKLOAD,
        runner_host_path=runner_path,
    )

    with pytest.raises(ValueError, match="requires a no-op source archive"):
        evaluator.prepare_framework_smoke(
            target=target,
            artifact=artifact,
            output_dir=tmp_path / "results",
            task_id=uuid4(),
            evaluation_run_id=uuid4(),
            attempt_number=1,
            baseline_request_id=uuid4(),
            noop_request_id=uuid4(),
            resource_id="hcu-7",
            fencing_token=1,
        )


def test_real_evaluator_builds_dual_attempt_evidence(tmp_path: Path) -> None:
    root = tmp_path / "attempt-0001"
    baseline_dir = root / "baseline"
    noop_dir = root / "noop"
    baseline_dir.mkdir(parents=True)
    noop_dir.mkdir()
    _write_variant(baseline_dir)
    _write_variant(noop_dir)
    baseline_request = ExecutionRequest(
        target_id=TARGET.target_id,
        argv=["python", "runner.py"],
        working_directory="/",
    )
    noop_request = ExecutionRequest(
        target_id=TARGET.target_id,
        argv=["python", "runner.py"],
        working_directory="/",
    )
    plan = SGLangSmokePlan(
        evidence_root=root,
        baseline_evidence_dir=baseline_dir,
        noop_evidence_dir=noop_dir,
        baseline_request=baseline_request,
        noop_request=noop_request,
    )
    candidate_id = uuid4()
    artifact = ArtifactManifest(
        candidate_id=candidate_id,
        kind="noop-source-archive",
        uri=(tmp_path / "noop.tar").as_uri(),
        content_hash="sha256:" + "a" * 64,
    )
    evaluator = SGLangSmokeEvaluator(workload_path=WORKLOAD)
    cleanup = {
        "fence": {"fenced": True},
        "health": {"healthy": True},
    }
    outcome = evaluator.evaluate_framework_smoke(
        plan,
        task_id=uuid4(),
        candidate_id=candidate_id,
        round_id=uuid4(),
        baseline_epoch_id=uuid4(),
        target=TARGET,
        target_fingerprint=_target_fingerprint(),
        artifact=artifact,
        evaluation_run_id=uuid4(),
        evidence_id=uuid4(),
        attempt_number=1,
        baseline_execution=_execution(baseline_request),
        noop_execution=_execution(noop_request),
        baseline_cleanup=cleanup,
        noop_cleanup=cleanup,
        adapter_provenance=[
            _provenance("executor"),
            evaluator.provenance,
            _provenance("resource_cleaner"),
        ],
        idempotency_key="fixture-paired-evaluation",
    )

    assert outcome.baseline_attempt.variant == "baseline"
    assert outcome.noop_attempt.variant == "noop"
    assert outcome.baseline_attempt.attempt_number == outcome.noop_attempt.attempt_number
    assert outcome.artifacts.evaluation.passed is True
    assert (root / "evidence-bundle.json").is_file()
    assert (root / "sha256sums.json").is_file()


def test_handler_runs_baseline_then_noop_and_returns_paired_contract(tmp_path: Path) -> None:
    candidate_id = uuid4()
    task_id = uuid4()
    round_id = uuid4()
    baseline_epoch_id = uuid4()
    evaluation_run_id = uuid4()
    artifact = ArtifactManifest(
        candidate_id=candidate_id,
        kind="noop-source-archive",
        uri=(tmp_path / "noop.tar").as_uri(),
        content_hash="sha256:" + "b" * 64,
    )

    class Executor:
        provenance = _provenance("executor")

        def __init__(self) -> None:
            self.requests = []

        def execute(self, request, target, output_dir):
            self.requests.append(request)
            return _execution(request)

        def cancel(self, request_id):
            return {"request_id": str(request_id), "owned_execution_found": False}

    class Cleaner:
        provenance = _provenance("resource_cleaner")

        def fence(self, resource_id, fencing_token):
            return {"resource_id": resource_id, "fencing_token": fencing_token, "fenced": True}

        def health_check(self, resource_id):
            return {"resource_id": resource_id, "healthy": True}

    class Evaluator:
        provenance = _provenance("evaluator")

        def prepare_framework_smoke(self, **values):
            common = {
                "target_id": TARGET.target_id,
                "argv": ["python", "runner.py"],
                "working_directory": "/",
                "lease_scope": LeaseScope.EXCLUSIVE,
                "resource_id": values["resource_id"],
                "fencing_token": values["fencing_token"],
                "container_image": TARGET.inference_image.immutable_reference,
            }
            return SimpleNamespace(
                baseline_request=ExecutionRequest(
                    request_id=values["baseline_request_id"], **common
                ),
                noop_request=ExecutionRequest(request_id=values["noop_request_id"], **common),
            )

        def evaluate_framework_smoke(self, plan, **values):
            baseline_attempt = ExecutionAttempt(
                evaluation_run_id=evaluation_run_id,
                request_id=plan.baseline_request.request_id,
                variant="baseline",
                attempt_number=values["attempt_number"],
                status="succeeded",
                exit_code=0,
                started_at=values["baseline_execution"].started_at,
                finished_at=values["baseline_execution"].finished_at,
                adapter_provenance=Executor.provenance,
            )
            noop_attempt = ExecutionAttempt(
                evaluation_run_id=evaluation_run_id,
                request_id=plan.noop_request.request_id,
                variant="noop",
                attempt_number=values["attempt_number"],
                status="succeeded",
                exit_code=0,
                started_at=values["noop_execution"].started_at,
                finished_at=values["noop_execution"].finished_at,
                adapter_provenance=Executor.provenance,
            )
            provenance = [Executor.provenance, self.provenance, Cleaner.provenance]
            evaluation = EvaluationRun(
                evaluation_run_id=evaluation_run_id,
                task_id=task_id,
                candidate_id=candidate_id,
                round_id=round_id,
                baseline_epoch_id=baseline_epoch_id,
                phase="correctness",
                protocol_version="framework-smoke-v1",
                target_fingerprint="sha256:" + "0" * 64,
                idempotency_key="fixture-paired",
                passed=True,
                metrics={"output_equivalent": True},
                adapter_provenance=provenance,
            )
            evidence = EvidenceBundle(
                task_id=task_id,
                candidate_id=candidate_id,
                baseline_epoch_id=baseline_epoch_id,
                target_id=TARGET.target_id,
                evidence_type="framework_smoke",
                protocol_version="framework-smoke-v1",
                artifact_ids=[artifact.artifact_id],
                summary={
                    "execution_attempt_ids": {
                        "baseline": str(baseline_attempt.execution_attempt_id),
                        "noop": str(noop_attempt.execution_attempt_id),
                    },
                    "performance_conclusion": "not_measured",
                },
                adapter_provenance=provenance,
            )
            return SimpleNamespace(
                baseline_attempt=baseline_attempt,
                noop_attempt=noop_attempt,
                artifacts=SimpleNamespace(evaluation=evaluation, evidence=evidence),
            )

    executor = Executor()
    registry = AdapterRegistry(
        profile=REAL_FRAMEWORK_SMOKE_PROFILE,
        executor=executor,
        evaluator=Evaluator(),
        resource_cleaner=Cleaner(),
    )
    handlers = JobHandlers(registry, tmp_path)
    raw = handlers.handle_framework_smoke(
        {
            "task_id": str(task_id),
            "candidate_id": str(candidate_id),
            "round_id": str(round_id),
            "baseline_epoch_id": str(baseline_epoch_id),
            "target": TARGET.model_dump(mode="json"),
            "target_fingerprint": "sha256:" + "0" * 64,
            "artifact": artifact.model_dump(mode="json"),
            "evaluation_run_id": str(evaluation_run_id),
            "baseline_execution_request_id": str(uuid4()),
            "noop_execution_request_id": str(uuid4()),
            "evidence_id": str(uuid4()),
            "retest_ordinal": 0,
            "_job_context": {
                "attempt_number": 1,
                "resource_id": "hcu-7",
                "fencing_token": 4,
            },
        }
    )
    result = PairedFrameworkSmokeResult.model_validate(raw)

    assert [item.variant for item in result.executions] == ["baseline", "noop"]
    assert [item.request_id for item in executor.requests] == [
        result.executions[0].execution_request.request_id,
        result.executions[1].execution_request.request_id,
    ]
    assert result.evaluation.passed is True
    assert result.evidence.summary["performance_conclusion"] == "not_measured"

    duplicate_request = result.model_dump(mode="json")
    duplicate_request["executions"][1]["execution_request"]["request_id"] = (
        duplicate_request["executions"][0]["execution_request"]["request_id"]
    )
    duplicate_request["executions"][1]["execution_result"]["request_id"] = (
        duplicate_request["executions"][0]["execution_request"]["request_id"]
    )
    duplicate_request["executions"][1]["execution_attempt"]["request_id"] = (
        duplicate_request["executions"][0]["execution_request"]["request_id"]
    )
    with pytest.raises(ValidationError, match="distinct execution request IDs"):
        PairedFrameworkSmokeResult.model_validate(duplicate_request)

    missing_variant = result.model_dump(mode="json")
    missing_variant["executions"] = missing_variant["executions"][:1]
    with pytest.raises(ValidationError, match="at least 2 items"):
        PairedFrameworkSmokeResult.model_validate(missing_variant)

    detached_cleanup = result.model_dump(mode="json")
    detached_cleanup["cleanup_evidence"]["variants"]["baseline"]["health"] = {
        "healthy": False
    }
    with pytest.raises(ValidationError, match="bind both variants"):
        PairedFrameworkSmokeResult.model_validate(detached_cleanup)

    unhealthy_pass = result.model_dump(mode="json")
    unhealthy_pass["executions"][0]["cleanup_evidence"]["health"]["healthy"] = False
    unhealthy_pass["cleanup_evidence"]["variants"]["baseline"]["health"][
        "healthy"
    ] = False
    with pytest.raises(ValidationError, match="healthy baseline cleanup"):
        PairedFrameworkSmokeResult.model_validate(unhealthy_pass)

    failed_execution_pass = result.model_dump(mode="json")
    failed_execution_pass["executions"][0]["execution_result"].update(
        {"status": "failed", "exit_code": 1}
    )
    failed_execution_pass["executions"][0]["execution_attempt"].update(
        {"status": "failed", "exit_code": 1}
    )
    with pytest.raises(ValidationError, match="successful baseline execution"):
        PairedFrameworkSmokeResult.model_validate(failed_execution_pass)


def test_handler_does_not_launch_noop_after_lease_loss(tmp_path: Path) -> None:
    lease_lost = threading.Event()
    candidate_id = uuid4()
    artifact = ArtifactManifest(
        candidate_id=candidate_id,
        kind="noop-source-archive",
        uri=(tmp_path / "noop.tar").as_uri(),
        content_hash="sha256:" + "c" * 64,
    )

    class LeaseLosingExecutor:
        provenance = _provenance("executor")

        def __init__(self) -> None:
            self.requests: list[ExecutionRequest] = []

        def execute(self, request, target, output_dir):
            self.requests.append(request)
            lease_lost.set()
            return _execution(request)

        def cancel(self, request_id):
            return {"request_id": str(request_id), "owned_execution_found": False}

    class Cleaner:
        provenance = _provenance("resource_cleaner")

        def __init__(self) -> None:
            self.fences = 0
            self.health_checks = 0

        def fence(self, resource_id, fencing_token):
            self.fences += 1
            return {"resource_id": resource_id, "fenced": True}

        def health_check(self, resource_id):
            self.health_checks += 1
            return {"resource_id": resource_id, "healthy": True}

    class Evaluator:
        provenance = _provenance("evaluator")

        def prepare_framework_smoke(self, **values):
            common = {
                "target_id": TARGET.target_id,
                "argv": ["python", "runner.py"],
                "working_directory": "/",
                "lease_scope": LeaseScope.EXCLUSIVE,
                "resource_id": values["resource_id"],
                "fencing_token": values["fencing_token"],
                "container_image": TARGET.inference_image.immutable_reference,
            }
            return SimpleNamespace(
                baseline_request=ExecutionRequest(
                    request_id=values["baseline_request_id"], **common
                ),
                noop_request=ExecutionRequest(
                    request_id=values["noop_request_id"], **common
                ),
            )

        def evaluate_framework_smoke(self, plan, **values):
            raise AssertionError("noop evaluation must not run after lease loss")

    executor = LeaseLosingExecutor()
    cleaner = Cleaner()
    handlers = JobHandlers(
        AdapterRegistry(
            profile=REAL_FRAMEWORK_SMOKE_PROFILE,
            executor=executor,
            evaluator=Evaluator(),
            resource_cleaner=cleaner,
        ),
        tmp_path,
    )
    payload = {
        "task_id": str(uuid4()),
        "candidate_id": str(candidate_id),
        "round_id": str(uuid4()),
        "baseline_epoch_id": str(uuid4()),
        "target": TARGET.model_dump(mode="json"),
        "target_fingerprint": _target_fingerprint(),
        "artifact": artifact.model_dump(mode="json"),
        "evaluation_run_id": str(uuid4()),
        "baseline_execution_request_id": str(uuid4()),
        "noop_execution_request_id": str(uuid4()),
        "evidence_id": str(uuid4()),
        "retest_ordinal": 0,
        "_job_context": {
            "attempt_number": 1,
            "resource_id": "hcu-7",
            "fencing_token": 5,
            "lease_lost_event": lease_lost,
        },
    }

    with pytest.raises(ExecutionSafetyError, match="lease was lost"):
        handlers.handle_framework_smoke(payload)

    assert len(executor.requests) == 1
    assert executor.requests[0].request_id == UUID(
        payload["baseline_execution_request_id"]
    )
    assert cleaner.fences == 1
    assert cleaner.health_checks == 1


def test_handler_does_not_launch_noop_after_unhealthy_baseline_cleanup(
    tmp_path: Path,
) -> None:
    candidate_id = uuid4()
    artifact = ArtifactManifest(
        candidate_id=candidate_id,
        kind="noop-source-archive",
        uri=(tmp_path / "noop.tar").as_uri(),
        content_hash="sha256:" + "d" * 64,
    )

    class Executor:
        provenance = _provenance("executor")

        def __init__(self) -> None:
            self.requests: list[ExecutionRequest] = []

        def execute(self, request, target, output_dir):
            self.requests.append(request)
            return _execution(request)

        def cancel(self, request_id):
            return {"request_id": str(request_id), "owned_execution_found": False}

    class Cleaner:
        provenance = _provenance("resource_cleaner")

        def fence(self, resource_id, fencing_token):
            return {"resource_id": resource_id, "fenced": False}

        def health_check(self, resource_id):
            return {"resource_id": resource_id, "healthy": False}

    class Evaluator:
        provenance = _provenance("evaluator")

        def prepare_framework_smoke(self, **values):
            common = {
                "target_id": TARGET.target_id,
                "argv": ["python", "runner.py"],
                "working_directory": "/",
                "lease_scope": LeaseScope.EXCLUSIVE,
                "resource_id": values["resource_id"],
                "fencing_token": values["fencing_token"],
                "container_image": TARGET.inference_image.immutable_reference,
            }
            return SimpleNamespace(
                baseline_request=ExecutionRequest(
                    request_id=values["baseline_request_id"], **common
                ),
                noop_request=ExecutionRequest(
                    request_id=values["noop_request_id"], **common
                ),
            )

        def evaluate_framework_smoke(self, plan, **values):
            raise AssertionError("no-op evaluation must not run after unhealthy cleanup")

    executor = Executor()
    handlers = JobHandlers(
        AdapterRegistry(
            profile=REAL_FRAMEWORK_SMOKE_PROFILE,
            executor=executor,
            evaluator=Evaluator(),
            resource_cleaner=Cleaner(),
        ),
        tmp_path,
    )
    payload = {
        "task_id": str(uuid4()),
        "candidate_id": str(candidate_id),
        "round_id": str(uuid4()),
        "baseline_epoch_id": str(uuid4()),
        "target": TARGET.model_dump(mode="json"),
        "target_fingerprint": _target_fingerprint(),
        "artifact": artifact.model_dump(mode="json"),
        "evaluation_run_id": str(uuid4()),
        "baseline_execution_request_id": str(uuid4()),
        "noop_execution_request_id": str(uuid4()),
        "evidence_id": str(uuid4()),
        "retest_ordinal": 0,
        "_job_context": {
            "attempt_number": 1,
            "resource_id": "hcu-7",
            "fencing_token": 6,
        },
    }

    with pytest.raises(ExecutionSafetyError, match="refusing to launch no-op"):
        handlers.handle_framework_smoke(payload)

    assert len(executor.requests) == 1
    assert executor.requests[0].request_id == UUID(
        payload["baseline_execution_request_id"]
    )
