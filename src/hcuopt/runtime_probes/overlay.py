from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from pathlib import Path
from time import monotonic
from typing import Any

from hcuopt.adapters.interfaces import ExecutionAdapter, ResourceCleaner
from hcuopt.contracts.platform_v1 import (
    ArtifactManifest,
    ExecutionRequest,
    MountSpec,
    SourceSnapshot,
    TargetSpec,
)
from hcuopt.domain.enums import HotPatchCapability, LeaseScope
from hcuopt.domain.errors import ExecutionSafetyError
from hcuopt.runtime_probes.evidence import sha256_file
from hcuopt.runtime_probes.profile import (
    OverlayPhaseConfiguration,
    OverlayProbeConfiguration,
)
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path

CACHE_ENVIRONMENT_KEY = "HCUOPT_CANDIDATE_CACHE_DIR"
OVERLAY_RESULT_PROTOCOL_VERSION = "hcuopt-overlay-result-v2"
MAX_OVERLAY_RESULT_BYTES = 64 * 1024


class OverlayCapabilityProbe:
    """Prove activation and recovery through three isolated container executions."""

    def __init__(self, executor: ExecutionAdapter, cleaner: ResourceCleaner) -> None:
        self.executor = executor
        self.cleaner = cleaner

    def run(
        self,
        configuration: Mapping[str, Any],
        *,
        target: TargetSpec,
        output_dir: Path,
        resource_id: str,
        fencing_token: int,
        max_wall_seconds: int | None = None,
    ) -> dict[str, Any]:
        if max_wall_seconds is not None and (
            isinstance(max_wall_seconds, bool) or max_wall_seconds < 1
        ):
            raise ValueError("overlay max_wall_seconds must be a positive integer")
        if not resource_id:
            raise ValueError("overlay probe requires a leased resource_id")
        if (
            isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token < 1
        ):
            raise ValueError("overlay probe requires a positive fencing_token")
        frozen = OverlayProbeConfiguration.model_validate(configuration)
        baseline = frozen.baseline_source
        candidate = frozen.candidate_source
        artifact = frozen.artifact
        activation_marker = frozen.activation_marker
        overlay_target = frozen.overlay_mount_target

        baseline_request = self._execution_request(
            frozen.baseline, target, resource_id, fencing_token
        )
        candidate_request = self._execution_request(
            frozen.candidate,
            target,
            resource_id,
            fencing_token,
            artifact=artifact,
            overlay_target=overlay_target,
        )
        recovery_request = self._execution_request(
            frozen.recovery, target, resource_id, fencing_token
        )

        self._validate_sources(baseline, candidate, artifact)
        self._validate_requests(
            target,
            artifact,
            frozen,
            overlay_target,
            baseline_request,
            candidate_request,
            recovery_request,
            resource_id,
            fencing_token,
        )
        baseline_path = file_uri_to_path(baseline.worktree_uri).resolve(strict=True)
        baseline_hash_before = canonical_source_hash(baseline_path)
        deadline = monotonic() + max_wall_seconds if max_wall_seconds is not None else None

        baseline_request = self._bounded_request(baseline_request, deadline)
        baseline_result = self.executor.execute(baseline_request, target, output_dir)
        baseline_metadata = self._execution_observation(baseline_result)
        baseline_health = dict(self.cleaner.health_check(resource_id))
        if baseline_result.status != "succeeded" or not baseline_health.get("healthy"):
            raise ExecutionSafetyError(
                "baseline execution or health check failed; refusing to launch candidate"
            )
        candidate_request = self._bounded_request(candidate_request, deadline)
        candidate_result = self.executor.execute(candidate_request, target, output_dir)
        candidate_metadata = self._execution_observation(candidate_result)
        candidate_health = dict(self.cleaner.health_check(resource_id))
        if not candidate_health.get("healthy"):
            raise ExecutionSafetyError(
                "candidate cleanup health check failed; resource must be quarantined"
            )
        recovery_request = self._bounded_request(recovery_request, deadline)
        recovery_result = self.executor.execute(recovery_request, target, output_dir)
        recovery_metadata = self._execution_observation(recovery_result)
        recovery_health = dict(self.cleaner.health_check(resource_id))
        baseline_hash_after = canonical_source_hash(baseline_path)

        execution_succeeded = all(
            item.status == "succeeded"
            for item in (baseline_result, candidate_result, recovery_result)
        )
        baseline_proved = (
            baseline_metadata.get("activation_marker") == "baseline"
            and baseline_metadata.get("loaded_artifact_hash") is None
        )
        activation_proved = (
            candidate_metadata.get("activation_marker") == activation_marker
            and candidate_metadata.get("loaded_artifact_hash") == artifact.content_hash
            and candidate_metadata.get("implementation_hash") == artifact.content_hash
            and candidate_metadata.get("replacement_point") == frozen.replacement_point
        )
        correctness_passed = baseline_metadata.get(
            "output_hash"
        ) is not None and candidate_metadata.get("output_hash") == baseline_metadata.get(
            "output_hash"
        )
        recovery_passed = (
            recovery_metadata.get("output_hash") == baseline_metadata.get("output_hash")
            and recovery_metadata.get("activation_marker") == "baseline"
            and recovery_metadata.get("loaded_artifact_hash") is None
            and recovery_metadata.get("implementation_hash")
            == baseline_metadata.get("implementation_hash")
            and recovery_metadata.get("replacement_point") == frozen.replacement_point
            and baseline_hash_before == baseline.source_hash == baseline_hash_after
        )
        resource_healthy = all(
            bool(item.get("healthy"))
            for item in (baseline_health, candidate_health, recovery_health)
        )
        observation_contract_matches = all(
            observation.get("workload_kind") == frozen.workload_kind
            and observation.get("replacement_point") == frozen.replacement_point
            for observation in (
                baseline_metadata,
                candidate_metadata,
                recovery_metadata,
            )
        )
        passed = all(
            (
                execution_succeeded,
                baseline_proved,
                activation_proved,
                correctness_passed,
                recovery_passed,
                resource_healthy,
                observation_contract_matches,
            )
        )
        sglang_overlay_proved = passed and frozen.workload_kind == "sglang_python_triton"
        capability = (
            HotPatchCapability.OVERLAY_ONLY if sglang_overlay_proved else HotPatchCapability.NONE
        )

        return {
            "capability": capability.value,
            "activation_mode": "startup_overlay",
            "workload_kind": frozen.workload_kind,
            "replacement_point": frozen.replacement_point,
            "generic_artifact_mount_passed": passed,
            "sglang_overlay_proved": sglang_overlay_proved,
            "execution_succeeded": execution_succeeded,
            "baseline_proved": baseline_proved,
            "activation_proved": activation_proved,
            "correctness_passed": correctness_passed,
            "recovery_passed": recovery_passed,
            "resource_healthy": resource_healthy,
            "observation_contract_matches": observation_contract_matches,
            "baseline_source_hash_before": baseline_hash_before,
            "baseline_source_hash_after": baseline_hash_after,
            "candidate_source_hash": candidate.source_hash,
            "artifact_hash": artifact.content_hash,
            "results": {
                "baseline": baseline_result.model_dump(mode="json"),
                "candidate": candidate_result.model_dump(mode="json"),
                "recovery": recovery_result.model_dump(mode="json"),
            },
            "observations": {
                "baseline": baseline_metadata,
                "candidate": candidate_metadata,
                "recovery": recovery_metadata,
            },
            "health": {
                "baseline": baseline_health,
                "candidate": candidate_health,
                "recovery": recovery_health,
            },
            "_formal_evidence": {
                "configuration": frozen,
                "requests": {
                    "baseline": baseline_request,
                    "candidate": candidate_request,
                    "recovery": recovery_request,
                },
                "results": {
                    "baseline": baseline_result,
                    "candidate": candidate_result,
                    "recovery": recovery_result,
                },
                "observations": {
                    "baseline": baseline_metadata,
                    "candidate": candidate_metadata,
                    "recovery": recovery_metadata,
                },
            },
        }

    @staticmethod
    def _bounded_request(request: ExecutionRequest, deadline: float | None) -> ExecutionRequest:
        if deadline is None:
            return request
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("runtime probe wall-clock budget exhausted")
        return request.model_copy(
            update={"timeout_seconds": max(1, min(request.timeout_seconds, math.ceil(remaining)))}
        )

    @staticmethod
    def _execution_observation(result: Any) -> dict[str, Any]:
        """Read the runner's bounded JSON result instead of trusting process exit alone.

        Test adapters may provide the observation directly in metadata. Real container
        executions must return the versioned object on stdout so activation and output
        equivalence are based on bytes preserved by the execution adapter.
        """

        if result.stdout_uri is None:
            value = dict(result.metadata)
        else:
            path = file_uri_to_path(result.stdout_uri).resolve(strict=True)
            if path.is_symlink() or not path.is_file():
                raise ExecutionSafetyError("overlay result stdout must be a regular file")
            if path.stat().st_size > MAX_OVERLAY_RESULT_BYTES:
                raise ExecutionSafetyError("overlay result stdout exceeds the 64 KiB limit")
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (UnicodeError, json.JSONDecodeError) as error:
                raise ExecutionSafetyError("overlay result stdout is not valid JSON") from error
        if not isinstance(value, dict):
            raise ExecutionSafetyError("overlay result stdout must contain one JSON object")
        if value.get("protocol_version") != OVERLAY_RESULT_PROTOCOL_VERSION:
            raise ExecutionSafetyError("overlay result protocol_version is invalid")
        marker = value.get("activation_marker")
        output_hash = value.get("output_hash")
        if not isinstance(marker, str) or not marker:
            raise ExecutionSafetyError("overlay result requires activation_marker")
        if not OverlayCapabilityProbe._is_sha256(output_hash):
            raise ExecutionSafetyError("overlay result requires a SHA256 output_hash")
        loaded_hash = value.get("loaded_artifact_hash")
        if loaded_hash is not None and not OverlayCapabilityProbe._is_sha256(loaded_hash):
            raise ExecutionSafetyError("overlay loaded_artifact_hash must be SHA256")
        workload_kind = value.get("workload_kind")
        if workload_kind not in {"generic_artifact_mount", "sglang_python_triton"}:
            raise ExecutionSafetyError("overlay result workload_kind is invalid")
        replacement_point = value.get("replacement_point")
        if not isinstance(replacement_point, str) or not replacement_point:
            raise ExecutionSafetyError("overlay result requires replacement_point")
        implementation_hash = value.get("implementation_hash")
        if not OverlayCapabilityProbe._is_sha256(implementation_hash):
            raise ExecutionSafetyError("overlay result requires implementation_hash")
        process_id = value.get("process_id")
        if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id < 1:
            raise ExecutionSafetyError("overlay result requires a positive process_id")
        return {
            "protocol_version": OVERLAY_RESULT_PROTOCOL_VERSION,
            "activation_marker": marker,
            "output_hash": output_hash,
            "workload_kind": workload_kind,
            "replacement_point": replacement_point,
            "implementation_hash": implementation_hash,
            "process_id": process_id,
            **({"loaded_artifact_hash": loaded_hash} if loaded_hash is not None else {}),
        }

    @staticmethod
    def _execution_request(
        phase: OverlayPhaseConfiguration,
        target: TargetSpec,
        resource_id: str,
        fencing_token: int,
        *,
        artifact: ArtifactManifest | None = None,
        overlay_target: str | None = None,
    ) -> ExecutionRequest:
        mounts = list(phase.mounts)
        if artifact is not None:
            if overlay_target is None:
                raise ValueError("candidate overlay requires a mount target")
            mounts.append(
                MountSpec(
                    source=OverlayCapabilityProbe._mount_source(
                        file_uri_to_path(artifact.uri).resolve(strict=True)
                    ),
                    target=overlay_target,
                    read_only=True,
                )
            )
        return ExecutionRequest(
            target_id=target.target_id,
            argv=list(phase.argv),
            working_directory=phase.working_directory,
            environment=phase.environment,
            timeout_seconds=phase.timeout_seconds,
            lease_scope=LeaseScope.EXCLUSIVE,
            resource_id=resource_id,
            fencing_token=fencing_token,
            container_image=target.inference_image.immutable_reference,
            mounts=mounts,
        )

    @staticmethod
    def _is_sha256(value: Any) -> bool:
        if not isinstance(value, str) or not value.startswith("sha256:"):
            return False
        digest = value.removeprefix("sha256:")
        return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)

    @staticmethod
    def _mount_source(path: Path) -> str:
        value = path.as_posix()
        return f"/{value}" if os.name == "nt" else value

    @staticmethod
    def _validate_sources(
        baseline: SourceSnapshot,
        candidate: SourceSnapshot,
        artifact: ArtifactManifest,
    ) -> None:
        if baseline.kind != "baseline" or not baseline.clean:
            raise ValueError("overlay probe requires a clean baseline snapshot")
        if candidate.kind != "candidate" or candidate.parent_snapshot_id != baseline.snapshot_id:
            raise ValueError("overlay candidate must be an independent child worktree")
        baseline_path = file_uri_to_path(baseline.worktree_uri).resolve(strict=True)
        candidate_path = file_uri_to_path(candidate.worktree_uri).resolve(strict=True)
        if baseline_path == candidate_path:
            raise ValueError("candidate worktree must be separate from the baseline")
        if canonical_source_hash(candidate_path) != candidate.source_hash:
            raise ValueError("candidate worktree no longer matches its SourceSnapshot")
        if artifact.source_snapshot_id != candidate.snapshot_id:
            raise ValueError("overlay artifact must reference the candidate source snapshot")
        artifact_path = file_uri_to_path(artifact.uri).resolve(strict=True)
        if artifact_path.is_symlink() or not artifact_path.is_file():
            raise ValueError("overlay artifact must be a regular immutable file")
        if sha256_file(artifact_path) != artifact.content_hash:
            raise ValueError("overlay artifact content hash does not match its manifest")
        if os.stat(artifact_path).st_mode & 0o222:
            raise ValueError("overlay artifact must be read-only")

    @staticmethod
    def _validate_requests(
        target: TargetSpec,
        artifact: ArtifactManifest,
        configuration: OverlayProbeConfiguration,
        overlay_target: str,
        baseline: ExecutionRequest,
        candidate: ExecutionRequest,
        recovery: ExecutionRequest,
        resource_id: str,
        fencing_token: int,
    ) -> None:
        for phase, request in zip(
            (configuration.baseline, configuration.candidate, configuration.recovery),
            (baseline, candidate, recovery),
            strict=True,
        ):
            if request.target_id != target.target_id:
                raise ValueError("overlay execution request targets a different Target Lock")
            if request.container_image != target.inference_image.immutable_reference:
                raise ValueError("overlay execution must use the digest-locked image")
            if request.resource_id != resource_id or request.fencing_token != fencing_token:
                raise ValueError("overlay execution must use the live lease and fencing token")
            OverlayCapabilityProbe._validate_formal_phase_mount(phase, request)

        if (
            configuration.workload_kind == "sglang_python_triton"
            and configuration.replacement_point != overlay_target
        ):
            raise ValueError("SGLang overlay must mount the artifact at its replacement point")

        artifact_path = OverlayCapabilityProbe._mount_source(
            file_uri_to_path(artifact.uri).resolve(strict=True)
        )
        matching_mounts = [
            mount
            for mount in candidate.mounts
            if mount.source == artifact_path and mount.target == overlay_target
        ]
        if len(matching_mounts) != 1 or not matching_mounts[0].read_only:
            raise ValueError("candidate artifact must be mounted exactly once and read-only")
        if any(mount.source == artifact_path for mount in (*baseline.mounts, *recovery.mounts)):
            raise ValueError("baseline and recovery requests cannot mount the candidate artifact")

        candidate_cache = candidate.environment.get(CACHE_ENVIRONMENT_KEY)
        if not candidate_cache:
            raise ValueError(f"candidate request requires {CACHE_ENVIRONMENT_KEY}")
        if candidate_cache in {
            baseline.environment.get(CACHE_ENVIRONMENT_KEY),
            recovery.environment.get(CACHE_ENVIRONMENT_KEY),
        }:
            raise ValueError("candidate cache must be isolated from baseline and recovery")

    @staticmethod
    def _validate_formal_phase_mount(
        phase: OverlayPhaseConfiguration,
        request: ExecutionRequest,
    ) -> None:
        if phase.evidence_directory_uri is None:
            return
        evidence_path = file_uri_to_path(phase.evidence_directory_uri)
        if evidence_path.is_symlink():
            raise ValueError("Formal overlay evidence directory cannot be a symlink")
        evidence_path = evidence_path.resolve(strict=True)
        if not evidence_path.is_dir():
            raise ValueError("Formal overlay evidence URI must name a directory")
        source = OverlayCapabilityProbe._mount_source(evidence_path)
        try:
            argument_index = request.argv.index("--evidence-dir")
            mount_target = request.argv[argument_index + 1]
        except (ValueError, IndexError) as exc:
            raise ValueError("Formal overlay argv must declare its evidence directory") from exc
        mounts = [
            mount
            for mount in request.mounts
            if mount.source == source and mount.target == mount_target
        ]
        if len(mounts) != 1 or mounts[0].read_only:
            raise ValueError("Formal overlay requires one worker-owned writable evidence mount")
        if "--formal-evidence" not in request.argv:
            raise ValueError("Formal overlay runner must enable raw evidence capture")
