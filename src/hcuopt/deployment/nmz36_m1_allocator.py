"""Real nmz36 adapters for the single M1 paged-allocator business Candidate."""

from __future__ import annotations

import hashlib
import json
import selectors
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from hcuopt.adapters.execution import FENCING_LABEL, MANAGED_LABEL, RESOURCE_LABEL
from hcuopt.adapters.m1_verification import (
    M1CorrectnessEvidenceProducer,
    M1CorrectnessEvidenceSubmission,
)
from hcuopt.adapters.resource_cleaner import ContainerResourceCleaner
from hcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    ArtifactManifest,
    SourceSnapshot,
    TargetSpec,
)
from hcuopt.domain.enums import LeaseScope
from hcuopt.domain.errors import ExecutionSafetyError
from hcuopt.evaluation.m1_protocol import (
    LoadedM1Protocol,
    M1CorrectnessCase,
    M1HotspotCorrectnessSpec,
    M1InputExpectation,
    M1OutputSpec,
    M1TensorSpec,
    m1_hotspot_spec_sha256,
)
from hcuopt.evaluation.m1_verifier import (
    M1CorrectnessBinding,
    M1CorrectnessEvidenceReference,
    M1CorrectnessEvidenceV1,
    M1ProcessEvidence,
    M1VerificationContext,
)
from hcuopt.measurement.evidence import write_evidence, write_evidence_bytes
from hcuopt.measurement.m1_allocator_reference import build_case, input_hash
from hcuopt.measurement.m1_harness import M1PairedWorkload, M1WorkloadFactory
from hcuopt.measurement.m1_models import M1ActivationEvidence, M1Arm
from hcuopt.measurement.models import (
    ProcessIdentity,
    ProcessLifecycleRecordV2,
    RawEvidenceFileV2,
)
from hcuopt.measurement.nmz36_runtime import (
    ManagedProcessRegistry,
    Nmz36WorkloadFactory,
)
from hcuopt.source_hash import file_uri_to_path
from hcuopt.targets import target_fingerprint

WORKER_PROTOCOL = "hcuopt-m1-allocator-worker-v1"
ALLOCATOR_RELATIVE_PATH = "python/sglang/srt/mem_cache/allocator.py"
ALLOCATOR_MOUNT_TARGET = (
    "/usr/local/lib/python3.10/dist-packages/sglang/srt/mem_cache/allocator.py"
)
ALLOCATOR_REPLACEMENT_POINT = (
    "sglang.srt.mem_cache.allocator.PagedTokenToKVPoolAllocator.free"
)


class Nmz36M1AllocatorError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _proc_start_token(stat_line: str, process_id: int) -> str:
    prefix = f"{process_id} ("
    if not stat_line.startswith(prefix):
        raise Nmz36M1AllocatorError("procfs identity does not match the measured process")
    closing = stat_line.rfind(")")
    fields = stat_line[closing + 2 :].split()
    if len(fields) < 20:
        raise Nmz36M1AllocatorError("procfs identity is incomplete")
    try:
        start_ticks = int(fields[19])
    except ValueError as exc:
        raise Nmz36M1AllocatorError("procfs starttime is not an integer") from exc
    if start_ticks < 1:
        raise Nmz36M1AllocatorError("procfs starttime must be positive")
    return f"linux-proc-startticks:{start_ticks}"


def _reference_file() -> Path:
    from hcuopt.measurement import m1_allocator_reference

    source = Path(m1_allocator_reference.__file__).resolve(strict=True)
    if source.is_symlink() or not source.is_file():
        raise Nmz36M1AllocatorError("M1 allocator reference source is not a regular file")
    return source


def _require_target(target: TargetSpec, deployment_policy: Any | None = None) -> None:
    if deployment_policy is not None:
        deployment_policy.validate_target(target)
        return
    if target.target_id != "nmz36-sglang-0.5.12":
        raise ValueError("M1 allocator deployment is locked to nmz36 SGLang 0.5.12")
    accelerator = target.execution_host.accelerator
    if (
        accelerator.device_index != 7
        or accelerator.numa_node != 7
        or accelerator.cpu_affinity != "112-127"
    ):
        raise ValueError("M1 allocator deployment topology differs from the Target Lock")


def _job_context(
    payload: Mapping[str, Any],
    expected_scope: LeaseScope,
    deployment_policy: Any | None = None,
) -> dict[str, Any]:
    if deployment_policy is not None:
        return deployment_policy.require_job_context(payload, expected_scope)
    raw = payload.get("_job_context")
    if not isinstance(raw, Mapping):
        raise ExecutionSafetyError("M1 allocator execution requires durable Job context")
    context = dict(raw)
    if context.get("lease_scope") != expected_scope.value:
        raise ExecutionSafetyError(
            f"M1 allocator execution requires a {expected_scope.value} lease"
        )
    resource_id = context.get("resource_id")
    fencing_token = context.get("fencing_token")
    lease_id = context.get("lease_id")
    if (
        resource_id != "hcu-7"
        or isinstance(fencing_token, bool)
        or not isinstance(fencing_token, int)
        or fencing_token < 1
        or not lease_id
    ):
        raise ExecutionSafetyError("M1 allocator lease binding is incomplete")
    return context


def _raw_reference(path: Path, expected_hash: str) -> RawEvidenceFileV2:
    if _sha256(path) != expected_hash:
        raise Nmz36M1AllocatorError(f"raw evidence hash mismatch: {path}")
    return RawEvidenceFileV2(uri=path.resolve(strict=True).as_uri(), sha256=expected_hash)


def _base_docker_argv(
    *,
    target: TargetSpec,
    source_root: Path,
    evidence_dir: Path,
    cache_dir: Path,
    container_name: str,
    resource_id: str,
    fencing_token: int,
    artifact: ArtifactManifest | None,
    deployment_policy: Any | None = None,
) -> list[str]:
    accelerator = target.execution_host.accelerator
    argv = [
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--pull=never",
        "--name",
        container_name,
        "--label",
        f"{MANAGED_LABEL}=true",
        "--label",
        f"{RESOURCE_LABEL}={resource_id}",
        "--label",
        f"{FENCING_LABEL}={fencing_token}",
    ]
    if deployment_policy is None:
        argv.extend(
            (
                "--security-opt",
                "no-new-privileges",
                "--pid=host",
                "--cpuset-cpus",
                accelerator.cpu_affinity,
                "--cpuset-mems",
                str(accelerator.numa_node),
                "--device",
                "/dev/kfd",
                "--device",
                "/dev/dri",
                "--group-add",
                "video",
                "--env",
                f"HIP_VISIBLE_DEVICES={accelerator.device_index}",
                "--env",
                "PYTHONPATH=/workspace/src",
            )
        )
    else:
        argv.extend(deployment_policy.docker_resource_arguments())
        for item in deployment_policy.container_environment():
            argv.extend(("--env", item))
    argv.extend(
        (
        "--mount",
        f"type=bind,src={source_root},dst=/workspace,readonly",
        "--mount",
        f"type=bind,src={evidence_dir},dst=/evidence",
        "--mount",
        f"type=bind,src={cache_dir},dst=/cache",
        "--mount",
        "type=bind,src=/opt/hyhal,dst=/opt/hyhal,readonly",
        )
    )
    if artifact is not None:
        argv.extend(
            (
                "--mount",
                f"type=bind,src={file_uri_to_path(artifact.uri)},"
                f"dst={ALLOCATOR_MOUNT_TARGET},readonly",
            )
        )
    argv.extend(
        (
            "--workdir",
            "/workspace",
            "--entrypoint",
            "python",
            target.inference_image.immutable_reference,
            "-m",
            "hcuopt.measurement.m1_allocator_worker",
        )
    )
    return argv


class _M1AllocatorContainerProcess:
    def __init__(
        self,
        *,
        target: TargetSpec,
        source_root: Path,
        evidence_dir: Path,
        cache_dir: Path,
        resource_id: str,
        fencing_token: int,
        arm: M1Arm,
        acquisition_ordinal: int,
        artifact: ArtifactManifest,
        harness_provenance: AdapterProvenance,
        workload_seed: int,
        registry: ManagedProcessRegistry,
        baseline_module_hash: str,
        deployment_policy: Any | None = None,
    ) -> None:
        self.arm = arm
        self.evidence_dir = evidence_dir.resolve(strict=True)
        self.container_name = f"hcuopt-m1-perf-{arm}-{uuid4().hex}"
        self.registry = registry
        self.stderr_path = self.evidence_dir / "container-stderr.log"
        self._stderr = self.stderr_path.open("wb")
        candidate_artifact = artifact if arm == "candidate" else None
        argv = _base_docker_argv(
            target=target,
            source_root=source_root,
            evidence_dir=self.evidence_dir,
            cache_dir=cache_dir,
            container_name=self.container_name,
            resource_id=resource_id,
            fencing_token=fencing_token,
            artifact=candidate_artifact,
            deployment_policy=deployment_policy,
        )
        argv.extend(
            (
                "--performance-controller",
                "--arm",
                arm,
                "--acquisition-ordinal",
                str(acquisition_ordinal),
                "--profile",
                harness_provenance.profile,
                "--harness-name",
                harness_provenance.adapter_name,
                "--harness-version",
                harness_provenance.adapter_version,
                "--image-digest",
                target.inference_image.registry_digest,
                "--cache-namespace",
                "/cache",
                "--cache-namespace-id",
                cache_dir.resolve(strict=True).as_uri(),
                "--evidence-dir",
                "/evidence",
                "--workload-seed",
                str(workload_seed),
            )
        )
        if arm == "candidate":
            argv.extend(("--expected-artifact-hash", artifact.content_hash))
        try:
            self._process = subprocess.Popen(
                tuple(argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                text=True,
                encoding="utf-8",
                bufsize=1,
                shell=False,
                start_new_session=True,
            )
            self.start_observation = self._read_response(180.0)
            self._validate_start(
                self.start_observation,
                artifact.content_hash if arm == "candidate" else baseline_module_hash,
            )
            self.process_id = int(self.start_observation["process_id"])
            self.process_start_token = _proc_start_token(
                str(self.start_observation["proc_stat_line"]), self.process_id
            )
            self.registry.add(self.process_id)
        except BaseException:
            self._force_remove()
            self._stderr.close()
            raise
        self.exit_observation: dict[str, Any] | None = None
        self._closed = False

    def request(self, payload: Mapping[str, Any], timeout_seconds: float = 180.0) -> dict[str, Any]:
        if self._closed:
            raise Nmz36M1AllocatorError("M1 allocator process is closed")
        assert self._process.stdin is not None
        self._process.stdin.write(
            json.dumps(
                dict(payload),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        self._process.stdin.flush()
        response = self._read_response(timeout_seconds)
        if response.get("event") == "error":
            raise Nmz36M1AllocatorError(str(response.get("error", "M1 worker failed")))
        return response

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.exit_observation = self.request({"op": "close"}, 60.0)
            returncode = self._process.wait(timeout=60)
            if returncode != 0:
                raise Nmz36M1AllocatorError(
                    f"M1 allocator worker exited with {returncode}; log={self.stderr_path}"
                )
        except BaseException:
            self._force_remove()
            raise
        finally:
            self._closed = True
            if hasattr(self, "process_id"):
                self.registry.discard(self.process_id)
            self._stderr.close()

    def force_close(self) -> None:
        if self._closed:
            return
        self._force_remove()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=5)
        self._closed = True
        if hasattr(self, "process_id"):
            self.registry.discard(self.process_id)
        self._stderr.close()

    def is_alive(self) -> bool:
        return not self._closed and self._process.poll() is None

    def _read_response(self, timeout_seconds: float) -> dict[str, Any]:
        assert self._process.stdout is not None
        selector = selectors.DefaultSelector()
        try:
            selector.register(self._process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout_seconds):
                raise Nmz36M1AllocatorError(
                    f"M1 allocator worker timed out; log={self.stderr_path}"
                )
            line = self._process.stdout.readline()
        finally:
            selector.close()
        if not line:
            raise Nmz36M1AllocatorError(
                f"M1 allocator worker closed stdout; exit={self._process.poll()}; "
                f"log={self.stderr_path}"
            )
        response = json.loads(line)
        if not isinstance(response, dict) or response.get("protocol") != WORKER_PROTOCOL:
            raise Nmz36M1AllocatorError("M1 allocator worker returned an invalid envelope")
        return response

    @staticmethod
    def _validate_start(response: Mapping[str, Any], expected_module_hash: str) -> None:
        process_id = response.get("process_id")
        if (
            response.get("event") != "ready"
            or isinstance(process_id, bool)
            or not isinstance(process_id, int)
            or process_id < 1
            or response.get("module_hash") != expected_module_hash
        ):
            raise Nmz36M1AllocatorError("M1 allocator worker activation is invalid")

    def _force_remove(self) -> None:
        subprocess.run(
            ("docker", "rm", "--force", self.container_name),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )


class Nmz36M1AllocatorPairedWorkload(M1PairedWorkload):
    def __init__(
        self,
        process: _M1AllocatorContainerProcess,
        *,
        target: TargetSpec,
        artifact: ArtifactManifest,
    ) -> None:
        self.process = process
        self.target = target
        self.artifact = artifact
        self.identity = ProcessIdentity(
            pid=process.process_id,
            start_token=process.process_start_token,
        )

    def process_identity(self) -> ProcessIdentity:
        return self.identity

    def activation_evidence(self) -> M1ActivationEvidence:
        ready = self.process.start_observation
        cache = _raw_reference(
            self.process.evidence_dir / "cache-namespace.json",
            str(ready["cache_sha256"]),
        )
        imported = None
        if self.process.arm == "candidate":
            imported = _raw_reference(
                self.process.evidence_dir / "import-attestation.json",
                str(ready["import_sha256"]),
            )
        return M1ActivationEvidence(
            arm=self.process.arm,
            activation_mode=(
                "startup_overlay" if self.process.arm == "candidate" else "baseline"
            ),
            image_digest=self.target.inference_image.registry_digest,
            loaded_artifact_hash=(
                self.artifact.content_hash if self.process.arm == "candidate" else None
            ),
            import_attestation=imported,
            cache_namespace_hash=str(ready["namespace_hash"]),
            cache_namespace_evidence=cache,
            cache_empty_before_execution=True,
        )

    def synchronize(self) -> None:
        self.process.request({"op": "synchronize"})

    def warmup(self) -> None:
        self.process.request({"op": "warmup", "iterations": 1})

    def measure_batch(self, iterations: int) -> RawEvidenceFileV2:
        response = self.process.request({"op": "measure", "iterations": iterations})
        ordinal = int(response["sample_ordinal"])
        return _raw_reference(
            self.process.evidence_dir / f"device-event-{ordinal:04d}.json",
            str(response["event_sha256"]),
        )

    def close(self) -> None:
        self.process.close()

    def is_alive(self) -> bool:
        return self.process.is_alive()


class Nmz36M1AllocatorWorkloadFactory(M1WorkloadFactory):
    def __init__(
        self,
        *,
        target: TargetSpec,
        source_root: Path,
        harness_provenance: AdapterProvenance,
        registry: ManagedProcessRegistry | None = None,
        workload_seed: int = 20260825,
        deployment_policy: Any | None = None,
    ) -> None:
        _require_target(target, deployment_policy)
        if harness_provenance.capability != "measurement_harness":
            raise ValueError("M1 allocator factory requires Measurement Harness provenance")
        self.target = target
        self.source_root = source_root.resolve(strict=True)
        self.harness_provenance = harness_provenance
        self.registry = registry or ManagedProcessRegistry()
        self.workload_seed = workload_seed
        self.deployment_policy = deployment_policy
        baseline = Path(target.source_baseline.clean_checkout) / ALLOCATOR_RELATIVE_PATH
        self.baseline_module_hash = _sha256(baseline.resolve(strict=True))

    def __call__(
        self,
        arm: M1Arm,
        acquisition_ordinal: int,
        payload: Mapping[str, Any],
        output_dir: Path,
    ) -> Nmz36M1AllocatorPairedWorkload:
        context = _job_context(payload, LeaseScope.EXCLUSIVE, self.deployment_policy)
        target = TargetSpec.model_validate(payload["target"])
        if target != self.target or payload.get("target_fingerprint") != target_fingerprint(target):
            raise ExecutionSafetyError("M1 allocator performance Target binding drifted")
        artifact = ArtifactManifest.model_validate(payload["artifact"])
        if artifact.synthetic or artifact.kind != "python_overlay":
            raise ExecutionSafetyError("M1 allocator performance requires a real Overlay Artifact")
        evidence_dir = (output_dir / f"acquisition-{acquisition_ordinal:04d}-{arm}").resolve()
        cache_dir = (output_dir / "cache" / f"acquisition-{acquisition_ordinal:04d}").resolve()
        if evidence_dir.exists() or cache_dir.exists():
            raise ExecutionSafetyError("M1 acquisition directories must be new")
        evidence_dir.mkdir(parents=True)
        cache_dir.mkdir(parents=True)
        process = _M1AllocatorContainerProcess(
            target=self.target,
            source_root=self.source_root,
            evidence_dir=evidence_dir,
            cache_dir=cache_dir,
            resource_id=str(context["resource_id"]),
            fencing_token=int(context["fencing_token"]),
            arm=arm,
            acquisition_ordinal=acquisition_ordinal,
            artifact=artifact,
            harness_provenance=self.harness_provenance,
            workload_seed=self.workload_seed,
            registry=self.registry,
            baseline_module_hash=self.baseline_module_hash,
            deployment_policy=self.deployment_policy,
        )
        return Nmz36M1AllocatorPairedWorkload(
            process,
            target=self.target,
            artifact=artifact,
        )


class Nmz36M1DeviceTimerFactory:
    """Create the calibration timer only after the exclusive Job owns HCU 7."""

    def __init__(
        self,
        *,
        target: TargetSpec,
        source_root: Path,
        registry: ManagedProcessRegistry | None = None,
        deployment_policy: Any | None = None,
    ) -> None:
        _require_target(target, deployment_policy)
        self.target = target
        self.source_root = source_root.resolve(strict=True)
        self.registry = registry or ManagedProcessRegistry()
        self.deployment_policy = deployment_policy

    def __call__(self, payload: Mapping[str, Any], output_dir: Path):  # type: ignore[no-untyped-def]
        context = _job_context(payload, LeaseScope.EXCLUSIVE, self.deployment_policy)
        factory = Nmz36WorkloadFactory(
            target=self.target,
            source_root=self.source_root,
            output_dir=(output_dir / "timer").resolve(),
            resource_id=str(context["resource_id"]),
            fencing_token=int(context["fencing_token"]),
            registry=self.registry,
        )
        return factory.timer()


class Nmz36M1AllocatorCorrectnessEvidenceProducer(M1CorrectnessEvidenceProducer):
    def __init__(
        self,
        *,
        target: TargetSpec,
        source_root: Path,
        protocol: LoadedM1Protocol,
        cleaner: ContainerResourceCleaner,
        deployment_policy: Any | None = None,
    ) -> None:
        _require_target(target, deployment_policy)
        self.target = target
        self.source_root = source_root.resolve(strict=True)
        self.protocol = protocol
        self.cleaner = cleaner
        self.deployment_policy = deployment_policy
        self.provenance = AdapterProvenance(
            profile=cleaner.provenance.profile,
            capability="correctness_evidence_producer",
            adapter_name=type(self).__name__,
            adapter_version="1",
            implementation_kind="real",
        )
        baseline = Path(target.source_baseline.clean_checkout) / ALLOCATOR_RELATIVE_PATH
        self.baseline_module_hash = _sha256(baseline.resolve(strict=True))

    def produce_manual_correctness_evidence(
        self,
        payload: Mapping[str, Any],
        context: M1VerificationContext,
        hotspot: M1HotspotCorrectnessSpec,
        output_dir: Path,
    ) -> M1CorrectnessEvidenceSubmission:
        job = _job_context(payload, LeaseScope.SHARED, self.deployment_policy)
        target = TargetSpec.model_validate(payload["target"])
        if (
            target != self.target
            or context.target_id != target.target_id
            or context.target_fingerprint != target_fingerprint(target)
        ):
            raise ExecutionSafetyError("M1 allocator correctness Target binding drifted")
        artifact = ArtifactManifest.model_validate(payload["artifact"])
        baseline = SourceSnapshot.model_validate(payload["baseline_source"])
        candidate = SourceSnapshot.model_validate(payload["candidate_source"])
        if (
            artifact.content_hash != context.artifact_hash
            or artifact.synthetic
            or artifact.kind != "python_overlay"
        ):
            raise ExecutionSafetyError("M1 allocator correctness Artifact binding drifted")
        if payload.get("replacement_point") not in {None, ALLOCATOR_REPLACEMENT_POINT}:
            raise ExecutionSafetyError("M1 allocator correctness replacement point drifted")

        run_root = (output_dir / "m1-correctness" / context.candidate_id.hex).resolve()
        if run_root.exists():
            raise ExecutionSafetyError("M1 correctness evidence root already exists")
        run_root.mkdir(parents=True)
        spec_file = write_evidence(run_root / "hotspot-spec.json", hotspot)
        if spec_file.sha256 != m1_hotspot_spec_sha256(hotspot):
            raise Nmz36M1AllocatorError("correctness spec publication changed its Hash")
        reference_source = _reference_file()
        if _sha256(reference_source) != hotspot.reference_source_hash:
            raise ExecutionSafetyError("M1 allocator reference source Hash drifted")

        cleanup: dict[str, Any]
        executions: list[M1ProcessEvidence] = []
        failure: BaseException | None = None
        try:
            executions.append(
                self._run_variant(
                    variant="reference",
                    ordinal=0,
                    artifact=None,
                    expected_module_hash=self.baseline_module_hash,
                    spec_bytes=file_uri_to_path(spec_file.uri).read_bytes(),
                    run_root=run_root,
                    context=context,
                )
            )
            executions.append(
                self._run_variant(
                    variant="candidate",
                    ordinal=1,
                    artifact=artifact,
                    expected_module_hash=artifact.content_hash,
                    spec_bytes=file_uri_to_path(spec_file.uri).read_bytes(),
                    run_root=run_root,
                    context=context,
                )
            )
        except BaseException as exc:
            failure = exc
        finally:
            cleanup = {
                "fence": self.cleaner.fence(
                    str(job["resource_id"]), int(job["fencing_token"])
                ),
                "health": self.cleaner.health_check(str(job["resource_id"])),
            }
        if failure is not None:
            write_evidence(
                run_root / "failure.json",
                {
                    "schema_version": "hcuopt-m1-correctness-failure-v1",
                    "error_type": type(failure).__name__,
                    "error": str(failure)[:2000],
                    "cleanup": cleanup,
                },
            )
            raise failure

        baseline_ref = write_evidence(run_root / "baseline-source.json", baseline)
        candidate_ref = write_evidence(run_root / "candidate-source.json", candidate)
        manifest_ref = write_evidence(run_root / "artifact-manifest.json", artifact)
        artifact_ref = write_evidence_bytes(
            run_root / "candidate-overlay.py",
            file_uri_to_path(artifact.uri).read_bytes(),
        )
        reference_ref = write_evidence_bytes(
            run_root / "allocator-reference.py", reference_source.read_bytes()
        )
        evidence = M1CorrectnessEvidenceV1(
            schema_version="m1-kernel-correctness-evidence-v1",
            binding=M1CorrectnessBinding(
                task_id=context.task_id,
                candidate_id=context.candidate_id,
                baseline_epoch_id=context.baseline_epoch_id,
                target_snapshot_id=context.target_snapshot_id,
                target_id=context.target_id,
                target_fingerprint=context.target_fingerprint,
                workload_id=context.workload_id,
                workload_hash=context.workload_hash,
                stage0_run_id=context.stage0_run_id,
                stage0_protocol_hash=context.stage0_protocol_hash,
                protocol_version=self.protocol.protocol.protocol_version,
                protocol_hash=self.protocol.protocol_hash,
                hotspot_spec_hash=m1_hotspot_spec_sha256(hotspot),
                lease_id=context.lease_id,
                lease_scope=context.lease_scope,
                resource_id=context.resource_id,
                fencing_token=context.fencing_token,
            ),
            baseline_source_snapshot=RawEvidenceFileV2(
                uri=baseline_ref.uri, sha256=baseline_ref.sha256
            ),
            candidate_source_snapshot=RawEvidenceFileV2(
                uri=candidate_ref.uri, sha256=candidate_ref.sha256
            ),
            reference_source=RawEvidenceFileV2(
                uri=reference_ref.uri, sha256=reference_ref.sha256
            ),
            artifact_manifest=RawEvidenceFileV2(
                uri=manifest_ref.uri, sha256=manifest_ref.sha256
            ),
            artifact=RawEvidenceFileV2(uri=artifact_ref.uri, sha256=artifact_ref.sha256),
            executions=tuple(executions),
            adapter_provenance=(self.provenance,),
            cleanup_evidence=cleanup,
            producer_summary={
                "business_candidate": True,
                "scope": "single_request_page_contiguous_allocator_free",
                "producer_verdict": None,
            },
        )
        published = write_evidence(run_root / "correctness.json", evidence)
        return M1CorrectnessEvidenceSubmission(
            reference=M1CorrectnessEvidenceReference(
                uri=published.uri, sha256=published.sha256
            ),
            cleanup_evidence=cleanup,
            adapter_provenance=(self.provenance,),
        )

    def _run_variant(
        self,
        *,
        variant: str,
        ordinal: int,
        artifact: ArtifactManifest | None,
        expected_module_hash: str,
        spec_bytes: bytes,
        run_root: Path,
        context: M1VerificationContext,
    ) -> M1ProcessEvidence:
        variant_root = run_root / variant
        cache_root = run_root / "cache" / variant
        variant_root.mkdir(parents=True)
        cache_root.mkdir(parents=True)
        write_evidence_bytes(variant_root / "hotspot-spec.json", spec_bytes)
        container_name = f"hcuopt-m1-correct-{variant}-{uuid4().hex}"
        argv = _base_docker_argv(
            target=self.target,
            source_root=self.source_root,
            evidence_dir=variant_root,
            cache_dir=cache_root,
            container_name=container_name,
            resource_id=context.resource_id,
            fencing_token=context.fencing_token,
            artifact=artifact,
            deployment_policy=self.deployment_policy,
        )
        argv.extend(
            (
                "--correctness-controller",
                "--variant",
                variant,
                "--profile",
                self.provenance.profile,
                "--image-digest",
                self.target.inference_image.registry_digest,
                "--cache-namespace",
                "/cache",
                "--cache-namespace-id",
                cache_root.resolve(strict=True).as_uri(),
                "--evidence-dir",
                "/evidence",
                "--spec",
                "/evidence/hotspot-spec.json",
            )
        )
        if artifact is not None:
            argv.extend(("--expected-artifact-hash", artifact.content_hash))
        completed = subprocess.run(
            tuple(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=self.protocol.protocol.max_execution_seconds,
            check=False,
            shell=False,
        )
        stdout_ref = write_evidence_bytes(variant_root / "stdout.log", completed.stdout)
        write_evidence_bytes(variant_root / "stderr.log", completed.stderr)
        observations = []
        for raw_line in completed.stdout.decode("utf-8", errors="strict").splitlines():
            value = json.loads(raw_line)
            if not isinstance(value, dict) or value.get("protocol") != WORKER_PROTOCOL:
                raise Nmz36M1AllocatorError("correctness worker returned an invalid envelope")
            observations.append(value)
        by_event = {str(item.get("event")): item for item in observations}
        started = by_event.get("started")
        reaped = by_event.get("reaped")
        complete = by_event.get("correctness_complete")
        if (
            completed.returncode != 0
            or started is None
            or reaped is None
            or complete is None
            or complete.get("module_hash") != expected_module_hash
        ):
            raise Nmz36M1AllocatorError(
                f"M1 {variant} correctness process failed with {completed.returncode}"
            )
        process_id = int(started["process_id"])
        start_token = _proc_start_token(str(started["proc_stat_line"]), process_id)
        start_record = ProcessLifecycleRecordV2(
            event="started",
            restart_ordinal=ordinal,
            observer_process_id=int(started["observer_process_id"]),
            process_id=process_id,
            proc_stat_line=str(started["proc_stat_line"]),
            captured_monotonic_ns=int(started["captured_monotonic_ns"]),
        )
        exit_record = ProcessLifecycleRecordV2(
            event="reaped",
            restart_ordinal=ordinal,
            observer_process_id=int(reaped["observer_process_id"]),
            process_id=process_id,
            proc_stat_line=str(reaped["proc_stat_line"]),
            captured_monotonic_ns=int(reaped["captured_monotonic_ns"]),
            waitpid_result_pid=int(reaped["waitpid_result_pid"]),
            wait_status=int(reaped["wait_status"]),
        )
        start_ref = write_evidence(variant_root / "process-start.json", start_record)
        exit_ref = write_evidence(variant_root / "process-exit.json", exit_record)
        normalized = variant_root / "normalized-output.json"
        cache = variant_root / "cache-namespace.json"
        return M1ProcessEvidence(
            variant=variant,
            process_id=process_id,
            process_start_token=start_token,
            start_record=RawEvidenceFileV2(uri=start_ref.uri, sha256=start_ref.sha256),
            exit_record=RawEvidenceFileV2(uri=exit_ref.uri, sha256=exit_ref.sha256),
            stdout=RawEvidenceFileV2(uri=stdout_ref.uri, sha256=stdout_ref.sha256),
            normalized_output=RawEvidenceFileV2(
                uri=normalized.resolve(strict=True).as_uri(), sha256=_sha256(normalized)
            ),
            cache_namespace=RawEvidenceFileV2(
                uri=cache.resolve(strict=True).as_uri(), sha256=_sha256(cache)
            ),
        )


def m1_allocator_reference_hash() -> str:
    return _sha256(_reference_file())


def build_m1_allocator_hotspot_spec(hotspot_id: str) -> M1HotspotCorrectnessSpec:
    case_ids = (
        "target-4091-direct-nosort",
        "target-4090-direct-nosort",
        "nonsorted-pages-direct-nosort",
        "sparse-pages-direct-nosort",
        "free-group-nosort",
        "nonsorted-pages-need-sort",
    )
    seed = 20260825
    cases = []
    expectations = []
    for case_id in case_ids:
        case = build_case(case_id, seed, "ordinary")
        cases.append(
            M1CorrectnessCase(
                case_id=case_id,
                inputs=(
                    M1TensorSpec(
                        name="free_index",
                        shape=(len(case.values),),
                        dtype="int64",
                    ),
                ),
                outputs=(
                    M1OutputSpec(
                        name="freed_pages",
                        shape=(len(case.unique_pages),),
                        dtype="int64",
                        atol=0,
                        rtol=0,
                        equal_nan=False,
                    ),
                    M1OutputSpec(
                        name="raw_freed_page_count",
                        shape=(1,),
                        dtype="int64",
                        atol=0,
                        rtol=0,
                        equal_nan=False,
                    ),
                    M1OutputSpec(
                        name="available_size",
                        shape=(1,),
                        dtype="int64",
                        atol=0,
                        rtol=0,
                        equal_nan=False,
                    ),
                ),
                seeds=(seed,),
                special_values=("ordinary",),
                repeats=2,
            )
        )
        expectations.append(
            M1InputExpectation(
                case_id=case_id,
                seed=seed,
                special_value="ordinary",
                input_hash=input_hash(case),
            )
        )
    return M1HotspotCorrectnessSpec(
        hotspot_id=hotspot_id,
        reference_implementation=(
            "Independent page-set and allocator-state oracle for the frozen single-request "
            "page-contiguous PagedTokenToKVPoolAllocator.free workload"
        ),
        reference_source_hash=m1_allocator_reference_hash(),
        cases=tuple(cases),
        input_expectations=tuple(expectations),
    )


__all__ = [
    "ALLOCATOR_MOUNT_TARGET",
    "ALLOCATOR_RELATIVE_PATH",
    "ALLOCATOR_REPLACEMENT_POINT",
    "Nmz36M1AllocatorCorrectnessEvidenceProducer",
    "Nmz36M1AllocatorPairedWorkload",
    "Nmz36M1AllocatorWorkloadFactory",
    "Nmz36M1DeviceTimerFactory",
    "build_m1_allocator_hotspot_spec",
    "m1_allocator_reference_hash",
]
