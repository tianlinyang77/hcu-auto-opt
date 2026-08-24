from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from f1c_helpers import create_repository, git, target_for

from hcuopt.adapters.build_cache import LocalBuildCache
from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.local_artifact_store import LocalArtifactStore
from hcuopt.adapters.manual_candidate import (
    CandidateSourcePackageStore,
    ManualOverlayCandidateBuilder,
)
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.contracts.m1 import CandidateSourcePackageManifest
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path
from hcuopt.workers.handlers import JobHandlers

PROFILE = "m1-candidate-builder-test-v1"
REPLACEMENT_PATH = "python/sglang/triton_kernel.py"
PROFILER_URI = "file:///trusted/profiler.json"
PROFILER_HASH = "sha256:" + "a" * 64
HOTSPOT_INTAKE_HASH = "sha256:" + "d" * 64


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _repository_with_overlay_point(path: Path) -> tuple[Path, str]:
    repository, _ = create_repository(path)
    source = repository / REPLACEMENT_PATH
    source.parent.mkdir(parents=True)
    source.write_text("MARKER = 'baseline'\n", encoding="utf-8")
    debug = repository / "tools/debug.py"
    debug.parent.mkdir(parents=True)
    debug.write_text("MARKER = 'debug-baseline'\n", encoding="utf-8")
    git(repository, "add", REPLACEMENT_PATH, "tools/debug.py")
    git(repository, "commit", "-m", "add overlay point")
    return repository, git(repository, "rev-parse", "HEAD")


def _candidate_hash(
    manager: GitSourceManager,
    baseline,  # type: ignore[no-untyped-def]
    candidate_id,  # type: ignore[no-untyped-def]
    output_dir: Path,
    replacement: bytes,
) -> str:
    preview = manager.create_candidate(baseline, candidate_id, output_dir)
    path = file_uri_to_path(preview.worktree_uri) / REPLACEMENT_PATH
    path.write_bytes(replacement)
    source_hash = canonical_source_hash(file_uri_to_path(preview.worktree_uri))
    manager.remove_candidate(baseline, preview, output_dir)
    return source_hash


def _publish_source_package(
    root: Path,
    *,
    candidate_id,  # type: ignore[no-untyped-def]
    hotspot_id,  # type: ignore[no-untyped-def]
    baseline_hash: str,
    candidate_hash: str,
    replacement: bytes,
    replacement_path: str = REPLACEMENT_PATH,
) -> CandidateSourcePackageManifest:
    digest = candidate_hash.removeprefix("sha256:")
    package = root / "sha256" / digest[:2] / digest[2:]
    source = package / "files" / replacement_path
    source.parent.mkdir(parents=True)
    source.write_bytes(replacement)
    manifest = CandidateSourcePackageManifest(
        candidate_id=candidate_id,
        hotspot_id=hotspot_id,
        baseline_source_hash=baseline_hash,
        candidate_source_hash=candidate_hash,
        replacement_point="sglang.triton_kernel",
        overlay_mount_target="/opt/sglang/python/sglang/triton_kernel.py",
        candidate_kind="fixture",
        files=[{"path": replacement_path, "content_hash": _sha256(replacement)}],
        profiler_evidence_uri=PROFILER_URI,
        profiler_evidence_hash=PROFILER_HASH,
        reviewed_by="reviewer",
        reviewed_at=datetime.now(timezone.utc),
    )
    (package / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    return manifest


def _builder(
    manager: GitSourceManager,
    trusted_root: Path,
    output_dir: Path,
) -> ManualOverlayCandidateBuilder:
    packages = CandidateSourcePackageStore(
        trusted_root,
        profile=PROFILE,
        allowed_overlay_roots=("python/sglang",),
        approved_mount_targets={
            "sglang.triton_kernel": "/opt/sglang/python/sglang/triton_kernel.py"
        },
    )
    return ManualOverlayCandidateBuilder(
        manager,
        packages,
        LocalArtifactStore(output_dir / "artifacts", PROFILE),
        LocalBuildCache(output_dir / "build-cache"),
        profile=PROFILE,
    )


def test_manual_build_creates_reproducible_read_only_overlay_without_dirtying_baseline(
    tmp_path: Path,
) -> None:
    repository, commit = _repository_with_overlay_point(tmp_path / "origin")
    output_dir = tmp_path / "run"
    manager = GitSourceManager(PROFILE)
    baseline = manager.prepare_baseline(
        target_for(repository, tmp_path / "baseline", commit), output_dir
    )
    candidate_id = uuid4()
    hotspot_id = uuid4()
    replacement = b"MARKER = 'candidate'\n"
    candidate_hash = _candidate_hash(
        manager, baseline, candidate_id, output_dir, replacement
    )
    _publish_source_package(
        tmp_path / "trusted-input",
        candidate_id=candidate_id,
        hotspot_id=hotspot_id,
        baseline_hash=baseline.source_hash,
        candidate_hash=candidate_hash,
        replacement=replacement,
    )
    builder = _builder(manager, tmp_path / "trusted-input", output_dir)
    handlers = JobHandlers(
        AdapterRegistry(profile=PROFILE, candidate_builder=builder), output_dir
    )
    payload = {
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
    }

    first = handlers.handle_manual_build(payload)
    second = handlers.handle_manual_build(payload)

    assert first["source"]["clean"] is True
    assert first["source"]["source_hash"] == candidate_hash
    assert first["artifact"]["kind"] == "python_overlay"
    assert first["artifact"]["artifact_id"] == second["artifact"]["artifact_id"]
    assert first["artifact"]["content_hash"] == second["artifact"]["content_hash"]
    artifact = file_uri_to_path(first["artifact"]["uri"])
    assert artifact.stat().st_mode & 0o222 == 0
    assert artifact.read_bytes() == replacement
    assert not any((output_dir / "worktrees").iterdir())
    baseline_path = file_uri_to_path(baseline.worktree_uri)
    assert canonical_source_hash(baseline_path) == baseline.source_hash
    assert git(baseline_path, "status", "--porcelain=v1") == ""


def test_manual_build_rejects_package_outside_approved_overlay_roots(
    tmp_path: Path,
) -> None:
    repository, commit = _repository_with_overlay_point(tmp_path / "origin")
    output_dir = tmp_path / "run"
    manager = GitSourceManager(PROFILE)
    baseline = manager.prepare_baseline(
        target_for(repository, tmp_path / "baseline", commit), output_dir
    )
    candidate_id = uuid4()
    hotspot_id = uuid4()
    replacement = b"not the approved module\n"
    preview = manager.create_candidate(baseline, candidate_id, output_dir)
    debug = file_uri_to_path(preview.worktree_uri) / "tools/debug.py"
    debug.write_bytes(replacement)
    candidate_hash = canonical_source_hash(file_uri_to_path(preview.worktree_uri))
    manager.remove_candidate(baseline, preview, output_dir)

    _publish_source_package(
        tmp_path / "trusted-input",
        candidate_id=candidate_id,
        hotspot_id=hotspot_id,
        baseline_hash=baseline.source_hash,
        candidate_hash=candidate_hash,
        replacement=replacement,
        replacement_path="tools/debug.py",
    )

    packages = CandidateSourcePackageStore(
        tmp_path / "trusted-input",
        profile=PROFILE,
        allowed_overlay_roots=("python/sglang",),
        approved_mount_targets={
            "sglang.triton_kernel": "/opt/sglang/python/sglang/triton_kernel.py"
        },
    )
    with pytest.raises(SourceArtifactError, match="outside deployment-approved"):
        packages.load(
            candidate_id=candidate_id,
            hotspot_id=hotspot_id,
            baseline_source_hash=baseline.source_hash,
            candidate_source_hash=candidate_hash,
            replacement_point="sglang.triton_kernel",
            candidate_kind="fixture",  # type: ignore[arg-type]
            profiler_evidence_uri=PROFILER_URI,
            profiler_evidence_hash=PROFILER_HASH,
        )


def test_manual_build_rejects_profiler_evidence_that_differs_from_hotspot_intake(
    tmp_path: Path,
) -> None:
    repository, commit = _repository_with_overlay_point(tmp_path / "origin")
    output_dir = tmp_path / "run"
    manager = GitSourceManager(PROFILE)
    baseline = manager.prepare_baseline(
        target_for(repository, tmp_path / "baseline", commit), output_dir
    )
    candidate_id = uuid4()
    hotspot_id = uuid4()
    replacement = b"MARKER = 'candidate'\n"
    candidate_hash = _candidate_hash(
        manager, baseline, candidate_id, output_dir, replacement
    )
    _publish_source_package(
        tmp_path / "trusted-input",
        candidate_id=candidate_id,
        hotspot_id=hotspot_id,
        baseline_hash=baseline.source_hash,
        candidate_hash=candidate_hash,
        replacement=replacement,
    )
    builder = _builder(manager, tmp_path / "trusted-input", output_dir)

    with pytest.raises(SourceArtifactError, match="does not match the durable M1 Job"):
        builder.build_candidate(
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
                    "profiler_raw_output_hash": "sha256:" + "b" * 64,
                },
            },
            output_dir,
        )
    assert not (output_dir / "worktrees").exists()


def test_manual_build_reads_artifact_from_frozen_candidate_not_mutated_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit = _repository_with_overlay_point(tmp_path / "origin")
    output_dir = tmp_path / "run"
    trusted_root = tmp_path / "trusted-input"
    manager = GitSourceManager(PROFILE)
    baseline = manager.prepare_baseline(
        target_for(repository, tmp_path / "baseline", commit), output_dir
    )
    candidate_id = uuid4()
    hotspot_id = uuid4()
    replacement = b"MARKER = 'candidate'\n"
    candidate_hash = _candidate_hash(
        manager, baseline, candidate_id, output_dir, replacement
    )
    _publish_source_package(
        trusted_root,
        candidate_id=candidate_id,
        hotspot_id=hotspot_id,
        baseline_hash=baseline.source_hash,
        candidate_hash=candidate_hash,
        replacement=replacement,
    )
    builder = _builder(manager, trusted_root, output_dir)
    original_apply = builder.source_packages.apply

    def mutate_after_apply(package, candidate_root):  # type: ignore[no-untyped-def]
        changed = original_apply(package, candidate_root)
        (package.files_root / REPLACEMENT_PATH).write_bytes(b"MUTATED AFTER SNAPSHOT APPLY\n")
        return changed

    monkeypatch.setattr(builder.source_packages, "apply", mutate_after_apply)
    result = builder.build_candidate(
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

    artifact = file_uri_to_path(result.artifact.uri)
    assert artifact.read_bytes() == replacement
    assert result.artifact.content_hash == _sha256(replacement)
