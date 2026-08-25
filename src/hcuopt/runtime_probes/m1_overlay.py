# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.interfaces import ResourceCleaner
from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    ArtifactManifest,
    SourceSnapshot,
    TargetSpec,
)
from hcuopt.domain.enums import LeaseScope
from hcuopt.domain.errors import ExecutionSafetyError, SourceIntegrityError
from hcuopt.measurement.evidence import canonical_json_bytes, write_evidence
from hcuopt.runtime_probes.overlay import CACHE_ENVIRONMENT_KEY, OverlayCapabilityProbe
from hcuopt.runtime_probes.profile import (
    OverlayPhaseConfiguration,
    OverlayProbeConfiguration,
)
from hcuopt.source_hash import file_uri_to_path
from hcuopt.targets import target_fingerprint


class ManualOverlayRuntimeProfile(ContractModel):
    """Deployment-owned commands and proven replacement points for M1-C."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: str = Field(min_length=1, max_length=200)
    target_id: str = Field(min_length=1, max_length=128)
    target_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    activation_marker: str = Field(min_length=1, max_length=200)
    replacement_points: dict[str, str] = Field(min_length=1)
    baseline: OverlayPhaseConfiguration
    candidate: OverlayPhaseConfiguration
    recovery: OverlayPhaseConfiguration

    @model_validator(mode="after")
    def require_isolated_candidate_and_restored_baseline_cache(
        self,
    ) -> ManualOverlayRuntimeProfile:
        baseline_cache = self.baseline.environment.get(CACHE_ENVIRONMENT_KEY)
        candidate_cache = self.candidate.environment.get(CACHE_ENVIRONMENT_KEY)
        recovery_cache = self.recovery.environment.get(CACHE_ENVIRONMENT_KEY)
        if not baseline_cache or baseline_cache != recovery_cache:
            raise ValueError("M1 Baseline and Recovery must use the same cache namespace")
        if not candidate_cache or candidate_cache == baseline_cache:
            raise ValueError("M1 Candidate must use an isolated cache namespace")
        if any(not value.startswith("/") for value in self.replacement_points.values()):
            raise ValueError("M1 replacement point mount targets must be absolute")
        return self


class ManualCandidateOverlayRuntime:
    """Materialize and attest Baseline/Candidate/Recovery in isolated processes."""

    def __init__(
        self,
        source_manager: GitSourceManager,
        overlay_probe: OverlayCapabilityProbe,
        cleaner: ResourceCleaner,
        target: TargetSpec,
        configuration: ManualOverlayRuntimeProfile,
        *,
        evidence_root: Path,
    ) -> None:
        expected_fingerprint = target_fingerprint(target)
        if configuration.target_id != target.target_id:
            raise ValueError("M1 runtime profile is bound to a different target")
        if configuration.target_fingerprint != expected_fingerprint:
            raise ValueError("M1 runtime profile fingerprint differs from the locked target")
        if source_manager.provenance.profile != configuration.profile:
            raise ValueError("M1 runtime source manager profile mismatch")
        if cleaner.provenance.profile != configuration.profile:
            raise ValueError("M1 runtime cleaner profile mismatch")
        self.source_manager = source_manager
        self.overlay_probe = overlay_probe
        self.cleaner = cleaner
        self.target = target
        self.configuration = configuration
        self.evidence_root = evidence_root.resolve()
        self.target_fingerprint = expected_fingerprint
        self.provenance = AdapterProvenance(
            profile=configuration.profile,
            capability="candidate_runtime",
            adapter_name=type(self).__name__,
            adapter_version="1.0.0",
            implementation_kind="real",
        )

    def verify(
        self,
        payload: Mapping[str, Any],
        output_dir: Path,
    ) -> dict[str, Any]:
        candidate_id = UUID(str(payload["candidate_id"]))
        hotspot_id = UUID(str(payload["hotspot_id"]))
        baseline = SourceSnapshot.model_validate(payload["baseline_source"])
        expected_candidate = SourceSnapshot.model_validate(payload["candidate_source"])
        artifact = ArtifactManifest.model_validate(payload["artifact"])
        context = payload.get("_job_context")
        if not isinstance(context, Mapping):
            raise ExecutionSafetyError("M1 Overlay runtime requires durable Job context")
        lease_id = context.get("lease_id")
        resource_id = context.get("resource_id")
        fencing_token = context.get("fencing_token")
        if (
            not isinstance(lease_id, str)
            or not lease_id
            or not isinstance(resource_id, str)
            or not resource_id
            or isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token < 1
        ):
            raise ExecutionSafetyError(
                "M1 Overlay runtime requires lease_id, resource_id and fencing_token"
            )
        if (
            payload.get("target_fingerprint") != self.target_fingerprint
            or TargetSpec.model_validate(payload["target"]) != self.target
        ):
            raise ExecutionSafetyError("M1 Overlay Job differs from the frozen Target")
        logical_replacement = str(payload["replacement_point"])
        mount_target = self.configuration.replacement_points.get(logical_replacement)
        if mount_target is None:
            raise ExecutionSafetyError("M1 Candidate replacement point is not deployment-approved")
        if (
            artifact.candidate_id != candidate_id
            or artifact.source_snapshot_id != expected_candidate.snapshot_id
            or artifact.kind != "python_overlay"
            or artifact.synthetic
            or artifact.metadata.get("source_hash") != expected_candidate.source_hash
            or artifact.metadata.get("hotspot_id") != str(hotspot_id)
            or artifact.metadata.get("hotspot_intake_hash")
            != payload.get("hotspot_intake_hash")
            or artifact.metadata.get("replacement_point") != logical_replacement
            or artifact.metadata.get("overlay_mount_target") != mount_target
            or artifact.metadata.get("candidate_kind") != payload.get("candidate_kind")
            or artifact.metadata.get("read_only") is not True
            or artifact.metadata.get("immutable") is not True
        ):
            raise ExecutionSafetyError("M1 Overlay Artifact has mismatched durable bindings")

        materialized = self.source_manager.create_candidate(
            baseline, candidate_id, output_dir
        )
        cleanup: dict[str, Any] | None = None
        try:
            relative_path = self._overlay_relative_path(artifact)
            self._replace_from_artifact(materialized, artifact, relative_path)
            finalized = self.source_manager.finalize_candidate(
                baseline,
                materialized,
                candidate_id,
                (relative_path,),
                expected_candidate.source_hash,
                output_dir,
            )
            self._require_same_candidate_source(expected_candidate, finalized)
            live_candidate = finalized.model_copy(
                update={"snapshot_id": expected_candidate.snapshot_id}
            )
            configuration = OverlayProbeConfiguration(
                workload_kind="sglang_python_triton",
                replacement_point=mount_target,
                baseline_source=baseline,
                candidate_source=live_candidate,
                artifact=artifact,
                overlay_mount_target=mount_target,
                activation_marker=self.configuration.activation_marker,
                baseline=self.configuration.baseline,
                candidate=self.configuration.candidate,
                recovery=self.configuration.recovery,
            )
            details = self.overlay_probe.run(
                configuration.model_dump(mode="json"),
                target=self.target,
                output_dir=output_dir,
                resource_id=resource_id,
                fencing_token=fencing_token,
                max_wall_seconds=self._max_wall_seconds(payload),
                lease_scope=LeaseScope.SHARED,
            )
            details.pop("_formal_evidence", None)
            process_identities = [
                {
                    "phase": name,
                    "execution_request_id": details["results"][name]["request_id"],
                    "container_process_id": details["observations"][name]["process_id"],
                }
                for name in ("baseline", "candidate", "recovery")
            ]
            # Linux PID namespaces commonly reuse the same numeric PID in separate
            # containers. ContainerExecutionAdapter creates one container per unique
            # ExecutionRequest, so request identity plus the attested in-container PID
            # is the correct cross-container process identity.
            independent_processes = len(
                {
                    (
                        item["execution_request_id"],
                        item["container_process_id"],
                    )
                    for item in process_identities
                }
            ) == len(process_identities)
            passed = bool(
                details["sglang_overlay_proved"]
                and details["recovery_passed"]
                and independent_processes
            )
            if not passed:
                raise ExecutionSafetyError(
                    "M1 Overlay did not prove activation, correctness, recovery, and "
                    "independent service processes"
                )
            cleanup = self._cleanup(resource_id, fencing_token)
            evidence_payload = {
                "protocol_version": "m1-overlay-runtime-v1",
                "candidate_id": str(candidate_id),
                "hotspot_id": str(hotspot_id),
                "hotspot_intake_hash": payload.get("hotspot_intake_hash"),
                "baseline_source_snapshot_id": str(baseline.snapshot_id),
                "candidate_source_snapshot_id": str(expected_candidate.snapshot_id),
                "artifact_id": str(artifact.artifact_id),
                "artifact_hash": artifact.content_hash,
                "target_fingerprint": self.target_fingerprint,
                "lease": {
                    "lease_id": lease_id,
                    "resource_id": resource_id,
                    "fencing_token": fencing_token,
                },
                "logical_replacement_point": logical_replacement,
                "overlay_mount_target": mount_target,
                "independent_processes": independent_processes,
                "process_identities": process_identities,
                "details": details,
                "cleanup_evidence": cleanup,
                "adapter_provenance": self.provenance.model_dump(mode="json"),
                "synthetic": False,
            }
            evidence = self._publish(candidate_id, evidence_payload)
            return {
                "candidate_id": str(candidate_id),
                "passed": True,
                "raw_evidence_uri": evidence.uri,
                "raw_evidence_hash": evidence.sha256,
                "summary": {
                    "activation_proved": True,
                    "correctness_observation_equal": True,
                    "recovery_passed": True,
                    "independent_processes": True,
                    "resource_healthy": True,
                },
                "cleanup_evidence": cleanup,
                "adapter_provenance": [self.provenance.model_dump(mode="json")],
                "synthetic": False,
            }
        except BaseException as runtime_error:
            if cleanup is None:
                try:
                    self._cleanup(resource_id, fencing_token)
                except Exception as cleanup_error:
                    raise cleanup_error from runtime_error
            raise
        finally:
            self.source_manager.remove_candidate(
                baseline, materialized, output_dir
            )

    @staticmethod
    def _overlay_relative_path(artifact: ArtifactManifest) -> str:
        raw = artifact.metadata.get("overlay_files")
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
            raise ExecutionSafetyError("M1 Artifact must name exactly one Overlay source file")
        path = raw[0].get("path")
        if not isinstance(path, str) or not path.endswith((".py", ".pyi")):
            raise ExecutionSafetyError("M1 Artifact Overlay path is invalid")
        parts = path.split("/")
        if (
            path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ExecutionSafetyError("M1 Artifact Overlay path escapes the Worktree")
        if raw[0].get("content_hash") != artifact.content_hash:
            raise ExecutionSafetyError("M1 Artifact Overlay file Hash is inconsistent")
        return path

    @staticmethod
    def _replace_from_artifact(
        candidate: SourceSnapshot,
        artifact: ArtifactManifest,
        relative_path: str,
    ) -> None:
        candidate_root = file_uri_to_path(candidate.worktree_uri).resolve(strict=True)
        destination = candidate_root / relative_path
        source = file_uri_to_path(artifact.uri).resolve(strict=True)
        if destination.is_symlink() or not destination.is_file():
            raise ExecutionSafetyError("M1 Artifact may replace only an existing source file")
        if source.is_symlink() or not source.is_file() or os.stat(source).st_mode & 0o222:
            raise ExecutionSafetyError("M1 Overlay Artifact must be a read-only regular file")
        if ManualCandidateOverlayRuntime._sha256(source) != artifact.content_hash:
            raise ExecutionSafetyError("M1 Overlay Artifact Hash mismatch")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{destination.name}.", dir=destination.parent, delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                with source.open("rb") as content:
                    while chunk := content.read(1024 * 1024):
                        temporary.write(chunk)
                temporary.flush()
                os.fsync(temporary.fileno())
            temporary_path.chmod(destination.stat().st_mode & 0o777)
            os.replace(temporary_path, destination)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _require_same_candidate_source(
        expected: SourceSnapshot,
        actual: SourceSnapshot,
    ) -> None:
        expected_identity = (
            expected.commit,
            expected.tree_hash,
            expected.source_hash,
            expected.clean,
            expected.parent_snapshot_id,
        )
        actual_identity = (
            actual.commit,
            actual.tree_hash,
            actual.source_hash,
            actual.clean,
            actual.parent_snapshot_id,
        )
        if actual_identity != expected_identity:
            raise SourceIntegrityError(
                "materialized Candidate does not reproduce its immutable SourceSnapshot"
            )

    def _cleanup(self, resource_id: str, fencing_token: int) -> dict[str, Any]:
        fence = dict(self.cleaner.fence(resource_id, fencing_token))
        health = dict(self.cleaner.health_check(resource_id))
        if fence.get("fenced") is not True or health.get("healthy") is not True:
            raise ExecutionSafetyError("M1 runtime cleanup or resource health check failed")
        return {"fence": fence, "health": health}

    def _publish(self, candidate_id: UUID, payload: dict[str, Any]):  # type: ignore[no-untyped-def]
        encoded = canonical_json_bytes(payload)
        digest = hashlib.sha256(encoded).hexdigest()
        destination = (
            self.evidence_root
            / "m1"
            / str(candidate_id)
            / "overlay-runtime"
            / f"sha256-{digest}.json"
        )
        return write_evidence(destination, payload)

    @staticmethod
    def _max_wall_seconds(payload: Mapping[str, Any]) -> int | None:
        budget = payload.get("budget", {})
        if not isinstance(budget, Mapping):
            raise ExecutionSafetyError("M1 Job budget must be an object")
        value = budget.get("max_wall_seconds")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ExecutionSafetyError("M1 max_wall_seconds must be a positive integer")
        return value

    @staticmethod
    def _sha256(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                hasher.update(chunk)
        return f"sha256:{hasher.hexdigest()}"
