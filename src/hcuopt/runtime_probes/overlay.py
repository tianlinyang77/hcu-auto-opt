from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hcuopt.adapters.interfaces import ExecutionAdapter, ResourceCleaner
from hcuopt.contracts.platform_v1 import (
    ArtifactManifest,
    ExecutionRequest,
    SourceSnapshot,
    TargetSpec,
)
from hcuopt.domain.enums import HotPatchCapability
from hcuopt.domain.errors import ExecutionSafetyError
from hcuopt.runtime_probes.evidence import sha256_file
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path

CACHE_ENVIRONMENT_KEY = "HCUOPT_CANDIDATE_CACHE_DIR"


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
    ) -> dict[str, Any]:
        baseline = SourceSnapshot.model_validate(configuration.get("baseline_source"))
        candidate = SourceSnapshot.model_validate(configuration.get("candidate_source"))
        artifact = ArtifactManifest.model_validate(configuration.get("artifact"))
        baseline_request = ExecutionRequest.model_validate(
            configuration.get("baseline_request")
        )
        candidate_request = ExecutionRequest.model_validate(
            configuration.get("candidate_request")
        )
        recovery_request = ExecutionRequest.model_validate(
            configuration.get("recovery_request")
        )
        activation_marker = self._required_text(configuration, "activation_marker")
        overlay_target = self._required_text(configuration, "overlay_mount_target")

        self._validate_sources(baseline, candidate, artifact)
        self._validate_requests(
            target,
            artifact,
            overlay_target,
            baseline_request,
            candidate_request,
            recovery_request,
            resource_id,
            fencing_token,
        )
        baseline_path = file_uri_to_path(baseline.worktree_uri).resolve(strict=True)
        baseline_hash_before = canonical_source_hash(baseline_path)

        baseline_result = self.executor.execute(baseline_request, target, output_dir)
        baseline_health = dict(self.cleaner.health_check(resource_id))
        if baseline_result.status != "succeeded" or not baseline_health.get("healthy"):
            raise ExecutionSafetyError(
                "baseline execution or health check failed; refusing to launch candidate"
            )
        candidate_result = self.executor.execute(candidate_request, target, output_dir)
        candidate_health = dict(self.cleaner.health_check(resource_id))
        if not candidate_health.get("healthy"):
            raise ExecutionSafetyError(
                "candidate cleanup health check failed; resource must be quarantined"
            )
        recovery_result = self.executor.execute(recovery_request, target, output_dir)
        recovery_health = dict(self.cleaner.health_check(resource_id))
        baseline_hash_after = canonical_source_hash(baseline_path)

        baseline_metadata = baseline_result.metadata
        candidate_metadata = candidate_result.metadata
        recovery_metadata = recovery_result.metadata
        execution_succeeded = all(
            item.status == "succeeded"
            for item in (baseline_result, candidate_result, recovery_result)
        )
        activation_proved = (
            candidate_metadata.get("activation_marker") == activation_marker
            and candidate_metadata.get("loaded_artifact_hash") == artifact.content_hash
        )
        correctness_passed = (
            baseline_metadata.get("output_hash") is not None
            and candidate_metadata.get("output_hash") == baseline_metadata.get("output_hash")
        )
        recovery_passed = (
            recovery_metadata.get("output_hash") == baseline_metadata.get("output_hash")
            and recovery_metadata.get("activation_marker") in (None, "baseline")
            and baseline_hash_before == baseline.source_hash == baseline_hash_after
        )
        resource_healthy = all(
            bool(item.get("healthy"))
            for item in (baseline_health, candidate_health, recovery_health)
        )
        passed = all(
            (
                execution_succeeded,
                activation_proved,
                correctness_passed,
                recovery_passed,
                resource_healthy,
            )
        )
        if not passed:
            capability = HotPatchCapability.NONE
        elif configuration.get("activation_mode") == "hot_patch" and bool(
            configuration.get("in_process_replacement_proved")
        ):
            capability = HotPatchCapability.HOT_PATCH
        else:
            capability = HotPatchCapability.OVERLAY_ONLY

        return {
            "capability": capability.value,
            "activation_mode": configuration.get("activation_mode", "startup_overlay"),
            "execution_succeeded": execution_succeeded,
            "activation_proved": activation_proved,
            "correctness_passed": correctness_passed,
            "recovery_passed": recovery_passed,
            "resource_healthy": resource_healthy,
            "baseline_source_hash_before": baseline_hash_before,
            "baseline_source_hash_after": baseline_hash_after,
            "candidate_source_hash": candidate.source_hash,
            "artifact_hash": artifact.content_hash,
            "results": {
                "baseline": baseline_result.model_dump(mode="json"),
                "candidate": candidate_result.model_dump(mode="json"),
                "recovery": recovery_result.model_dump(mode="json"),
            },
            "health": {
                "baseline": baseline_health,
                "candidate": candidate_health,
                "recovery": recovery_health,
            },
        }

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
        overlay_target: str,
        baseline: ExecutionRequest,
        candidate: ExecutionRequest,
        recovery: ExecutionRequest,
        resource_id: str,
        fencing_token: int,
    ) -> None:
        for request in (baseline, candidate, recovery):
            if request.target_id != target.target_id:
                raise ValueError("overlay execution request targets a different Target Lock")
            if request.container_image != target.inference_image.immutable_reference:
                raise ValueError("overlay execution must use the digest-locked image")
            if request.resource_id != resource_id or request.fencing_token != fencing_token:
                raise ValueError("overlay execution must use the live lease and fencing token")

        artifact_path = file_uri_to_path(artifact.uri).resolve(strict=True).as_posix()
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
    def _required_text(configuration: Mapping[str, Any], name: str) -> str:
        value = configuration.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"overlay configuration requires {name}")
        return value
