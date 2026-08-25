from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from hcuopt.adapters.build_cache import LocalBuildCache
from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.local_artifact_store import LocalArtifactStore
from hcuopt.adapters.manual_candidate import (
    CandidateSourcePackageStore,
    ManualOverlayCandidateBuilder,
)
from hcuopt.contracts.m1 import CandidateSourcePackageManifest
from hcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    ExecutionRequest,
    ExecutionResult,
    TargetSpec,
)
from hcuopt.runtime_probes.m1_overlay import (
    ManualCandidateOverlayRuntime,
    ManualOverlayRuntimeProfile,
)
from hcuopt.runtime_probes.overlay import OverlayCapabilityProbe
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path
from hcuopt.targets import load_target, target_fingerprint

ROOT = Path(__file__).parents[2]
PROFILE = "m1-c-scripted-v1"
SOURCE_PATH = "python/sglang/triton_kernel.py"
MOUNT_TARGET = "/opt/sglang/python/sglang/triton_kernel.py"
ACTIVATION_MARKER = "m1-scripted-candidate-v1"
PROFILER_URI = "file:///trusted/profiler.json"
PROFILER_HASH = "sha256:" + "a" * 64
HOTSPOT_INTAKE_HASH = "sha256:" + "d" * 64


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _repository(path: Path) -> tuple[Path, str]:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.name", "M1-C Scripted")
    _git(path, "config", "user.email", "m1-c@example.invalid")
    source = path / SOURCE_PATH
    source.parent.mkdir(parents=True)
    source.write_text("MARKER = 'baseline'\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "baseline")
    return path, _git(path, "rev-parse", "HEAD")


def _target(repository: Path, checkout: Path, commit: str) -> TargetSpec:
    target = load_target(ROOT / "config/targets/nmz36-sglang-0.5.12.yaml")
    source = target.source_baseline.model_copy(
        update={
            "repository": str(repository),
            "branch": "main",
            "commit": commit,
            "branch_head_observed_at_lock": commit,
            "clean_checkout": str(checkout),
        }
    )
    return target.model_copy(update={"source_baseline": source})


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


class ScriptedCleaner:
    def __init__(self) -> None:
        self.provenance = AdapterProvenance(
            profile=PROFILE,
            capability="resource_cleaner",
            adapter_name=type(self).__name__,
            adapter_version="1",
            implementation_kind="real",
        )
        self.fence_calls: list[tuple[str, int]] = []

    def fence(self, resource_id: str, fencing_token: int):  # type: ignore[no-untyped-def]
        self.fence_calls.append((resource_id, fencing_token))
        return {
            "resource_id": resource_id,
            "fencing_token": fencing_token,
            "fenced": True,
        }

    def health_check(self, resource_id: str):  # type: ignore[no-untyped-def]
        return {"resource_id": resource_id, "healthy": True}


class ScriptedExecutor:
    def __init__(self, artifact_hash: str, candidate_id: UUID) -> None:
        self.artifact_hash = artifact_hash
        self.candidate_id = candidate_id
        self.calls = 0
        self.provenance = AdapterProvenance(
            profile=PROFILE,
            capability="executor",
            adapter_name=type(self).__name__,
            adapter_version="1",
            implementation_kind="real",
        )

    def execute(
        self,
        request: ExecutionRequest,
        target: TargetSpec,
        output_dir: Path,
    ) -> ExecutionResult:
        del target, output_dir
        self.calls += 1
        candidate = any(mount.target == MOUNT_TARGET for mount in request.mounts)
        now = datetime.now(timezone.utc)
        baseline_implementation = "sha256:" + "b" * 64
        observation = {
            "protocol_version": "hcuopt-overlay-result-v2",
            "activation_marker": (
                ACTIVATION_MARKER if candidate else "baseline"
            ),
            "output_hash": "sha256:" + "c" * 64,
            "workload_kind": "sglang_python_triton",
            "replacement_point": MOUNT_TARGET,
            "implementation_hash": (
                self.artifact_hash if candidate else baseline_implementation
            ),
            # Separate container PID namespaces may legitimately reuse PID 11.
            "process_id": 11,
        }
        if candidate:
            observation["loaded_artifact_hash"] = self.artifact_hash
        return ExecutionResult(
            request_id=request.request_id,
            status="succeeded",
            exit_code=0,
            started_at=now,
            finished_at=now,
            metadata=observation,
            adapter_provenance=self.provenance,
            synthetic=False,
        )

    def cancel(self, request_id: UUID):  # type: ignore[no-untyped-def]
        return {"request_id": str(request_id), "cancelled": True}


def test_m1_c_scripted_build_activation_and_recovery_chain(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path / "origin")
    target = _target(repository, tmp_path / "baseline", commit)
    output_dir = tmp_path / "run"
    trusted_root = tmp_path / "trusted-input"
    manager = GitSourceManager(PROFILE)
    baseline = manager.prepare_baseline(target, output_dir)
    candidate_id = uuid4()
    hotspot_id = uuid4()
    replacement = b"MARKER = 'candidate'\n"

    preview = manager.create_candidate(baseline, candidate_id, output_dir)
    (file_uri_to_path(preview.worktree_uri) / SOURCE_PATH).write_bytes(replacement)
    candidate_hash = canonical_source_hash(file_uri_to_path(preview.worktree_uri))
    manager.remove_candidate(baseline, preview, output_dir)

    digest = candidate_hash.removeprefix("sha256:")
    package = trusted_root / "sha256" / digest[:2] / digest[2:]
    package_source = package / "files" / SOURCE_PATH
    package_source.parent.mkdir(parents=True)
    package_source.write_bytes(replacement)
    manifest = CandidateSourcePackageManifest(
        candidate_id=candidate_id,
        hotspot_id=hotspot_id,
        baseline_source_hash=baseline.source_hash,
        candidate_source_hash=candidate_hash,
        replacement_point="sglang.triton_kernel",
        overlay_mount_target=MOUNT_TARGET,
        candidate_kind="fixture",
        files=[{"path": SOURCE_PATH, "content_hash": _sha256(replacement)}],
        profiler_evidence_uri=PROFILER_URI,
        profiler_evidence_hash=PROFILER_HASH,
        reviewed_by="reviewer",
        reviewed_at=datetime.now(timezone.utc),
    )
    (package / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    packages = CandidateSourcePackageStore(
        trusted_root,
        profile=PROFILE,
        allowed_overlay_roots=("python/sglang",),
        approved_mount_targets={"sglang.triton_kernel": MOUNT_TARGET},
    )
    builder = ManualOverlayCandidateBuilder(
        manager,
        packages,
        LocalArtifactStore(output_dir / "artifacts", PROFILE),
        LocalBuildCache(output_dir / "build-cache"),
        profile=PROFILE,
    )
    build = builder.build_candidate(
        {
            "candidate_id": str(candidate_id),
            "hotspot_id": str(hotspot_id),
            "baseline_source": baseline.model_dump(mode="json"),
            "candidate_source_hash": candidate_hash,
            "replacement_point": "sglang.triton_kernel",
            "candidate_kind": "fixture",
            "hotspot_intake_hash": HOTSPOT_INTAKE_HASH,
            "hotspot": {
                "profiler_raw_output_uri": PROFILER_URI,
                "profiler_raw_output_hash": PROFILER_HASH,
            },
        },
        output_dir,
    )

    cleaner = ScriptedCleaner()
    executor = ScriptedExecutor(build.artifact.content_hash, candidate_id)
    phase = lambda name, cache: {  # noqa: E731
        "argv": ["python", "-m", "fixed_sglang_runner", name],
        "working_directory": "/workspace",
        "environment": {"HCUOPT_CANDIDATE_CACHE_DIR": cache},
    }
    runtime = ManualCandidateOverlayRuntime(
        manager,
        OverlayCapabilityProbe(executor, cleaner),
        cleaner,
        target,
        ManualOverlayRuntimeProfile(
            profile=PROFILE,
            target_id=target.target_id,
            target_fingerprint=target_fingerprint(target),
            activation_marker=ACTIVATION_MARKER,
            replacement_points={"sglang.triton_kernel": MOUNT_TARGET},
            baseline=phase("baseline", "/cache/baseline"),
            candidate=phase("candidate", "/cache/candidate"),
            recovery=phase("recovery", "/cache/baseline"),
        ),
        evidence_root=tmp_path / "trusted-evidence",
    )
    result = runtime.verify(
        {
            "candidate_id": str(candidate_id),
            "hotspot_id": str(hotspot_id),
            "hotspot_intake_hash": HOTSPOT_INTAKE_HASH,
            "baseline_source": baseline.model_dump(mode="json"),
            "candidate_source": build.source.model_dump(mode="json"),
            "artifact": build.artifact.model_dump(mode="json"),
            "replacement_point": "sglang.triton_kernel",
            "candidate_kind": "fixture",
            "target": target.model_dump(mode="json"),
            "target_fingerprint": target_fingerprint(target),
            "budget": {"max_wall_seconds": 60},
            "_job_context": {
                "lease_id": str(uuid4()),
                "resource_id": "hcu-7",
                "fencing_token": 9,
            },
        },
        output_dir,
    )

    assert result["passed"] is True
    assert result["summary"]["independent_processes"] is True
    assert result["summary"]["recovery_passed"] is True
    assert result["cleanup_evidence"]["health"]["healthy"] is True
    assert file_uri_to_path(result["raw_evidence_uri"]).is_file()
    assert executor.calls == 3
    assert cleaner.fence_calls == [("hcu-7", 9)]
    assert not any((output_dir / "worktrees").iterdir())
    assert canonical_source_hash(file_uri_to_path(baseline.worktree_uri)) == baseline.source_hash
