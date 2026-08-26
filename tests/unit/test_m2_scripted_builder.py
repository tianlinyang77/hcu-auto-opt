# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from f1c_helpers import git, target_for
from pydantic import ValidationError

from hcuopt.adapters.build_cache import LocalBuildCache
from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.local_artifact_store import LocalArtifactStore
from hcuopt.adapters.m2_candidate import (
    SCRIPTED_CANDIDATE_FIXTURES,
    ScriptedCandidateIntake,
    candidate_source_package_hash,
)
from hcuopt.adapters.m2_scripted_builder import ScriptedRoundCandidateBuilder
from hcuopt.adapters.manual_candidate import CandidateSourcePackageStore
from hcuopt.contracts.m1 import CandidateSourcePackageManifest
from hcuopt.contracts.m2 import RoundBudget, ScriptedCandidatePackageInput, SearchRound
from hcuopt.domain.errors import ArtifactIntegrityError, SourceArtifactError
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.orchestrator.search_round import candidate_family_hash
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path

PROFILE = "m2-scripted-builder-test-v1"
STORE_ID = "m2-scripted-package-store-v1"
STORE_HASH = "sha256:" + "c" * 64
REPLACEMENT_POINT = "sglang.fixture_kernel"
REPLACEMENT_PATH = "python/sglang/fixture_kernel.py"
MOUNT_TARGET = "/opt/sglang/python/sglang/fixture_kernel.py"
PROFILER_URI = "fixture:///m2/profiler.json"
PROFILER_HASH = "sha256:" + "d" * 64

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="Git Worktree and immutable Artifact semantics are validated on Linux CI",
)


def _hash(value: str) -> str:
    return "sha256:" + value * 64


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _repository(path: Path) -> tuple[Path, str]:
    path.mkdir(parents=True)
    git(path, "init")
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.com")
    source = path / REPLACEMENT_PATH
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 0\n", encoding="utf-8")
    git(path, "add", REPLACEMENT_PATH)
    git(path, "commit", "-m", "add scripted fixture point")
    return path, git(path, "rev-parse", "HEAD")


def _round(hotspot_id) -> SearchRound:  # type: ignore[no-untyped-def]
    return SearchRound(
        round_id=uuid4(),
        task_id=uuid4(),
        idempotency_key="m2-scripted-build-round",
        state="intake_open",
        run_mode="scripted",
        project_mode=None,
        target_snapshot_id=uuid4(),
        stage0_run_id=uuid4(),
        stage0_protocol_hash=_hash("1"),
        baseline_epoch_id=uuid4(),
        hotspot_id=hotspot_id,
        replacement_point=REPLACEMENT_POINT,
        workload_id="m2-scripted-workload-v1",
        workload_hash=_hash("2"),
        configuration_hash=_hash("3"),
        image_digest=_hash("4"),
        adapter_profile=PROFILE,
        declared_candidate_count=4,
        max_promoted=2,
        family_alpha=0.05,
        search_plan_hash=_hash("5"),
        holdout_plan_commitment=_hash("6"),
        holdout_plan_authority_id="synthetic-holdout-v1",
        holdout_plan_authority_hash=_hash("7"),
        selection_rule_hash=_hash("8"),
        budget=RoundBudget(
            max_candidates=4,
            max_build_attempts=4,
            max_correctness_attempts=4,
            max_search_samples=400,
            max_holdout_samples=400,
            max_wall_seconds=600,
            max_exclusive_lease_seconds=300,
        ),
        version=1,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _candidate_hash(
    manager: GitSourceManager,
    baseline,  # type: ignore[no-untyped-def]
    candidate_id,  # type: ignore[no-untyped-def]
    output_dir: Path,
    replacement: bytes,
) -> str:
    preview = manager.create_candidate(baseline, candidate_id, output_dir)
    (file_uri_to_path(preview.worktree_uri) / REPLACEMENT_PATH).write_bytes(replacement)
    source_hash = canonical_source_hash(file_uri_to_path(preview.worktree_uri))
    manager.remove_candidate(baseline, preview, output_dir)
    return source_hash


def _publish_package(
    root: Path,
    *,
    candidate_id,  # type: ignore[no-untyped-def]
    hotspot_id,  # type: ignore[no-untyped-def]
    baseline_hash: str,
    candidate_hash: str,
    replacement: bytes,
) -> tuple[str, str]:
    manifest = CandidateSourcePackageManifest(
        candidate_id=candidate_id,
        hotspot_id=hotspot_id,
        baseline_source_hash=baseline_hash,
        candidate_source_hash=candidate_hash,
        replacement_point=REPLACEMENT_POINT,
        candidate_kind="fixture",
        overlay_mount_target=MOUNT_TARGET,
        files=[{"path": REPLACEMENT_PATH, "content_hash": _sha256(replacement)}],
        profiler_evidence_uri=PROFILER_URI,
        profiler_evidence_hash=PROFILER_HASH,
        reviewed_by="m2-scripted-fixture-authority",
        reviewed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    manifest_bytes = canonical_json_bytes(manifest)
    manifest_hash = _sha256(manifest_bytes)
    package_hash = candidate_source_package_hash(manifest_hash, manifest.files)
    digest = candidate_hash.removeprefix("sha256:")
    package_root = root / "sha256" / digest[:2] / digest[2:]
    source = package_root / "files" / REPLACEMENT_PATH
    source.parent.mkdir(parents=True)
    source.write_bytes(replacement)
    (package_root / "manifest.json").write_bytes(manifest_bytes)
    return manifest_hash, package_hash


def _prepare_family(tmp_path: Path):  # type: ignore[no-untyped-def]
    repository, commit = _repository(tmp_path / "origin")
    output_dir = tmp_path / "run"
    manager = GitSourceManager(PROFILE)
    baseline = manager.prepare_baseline(
        target_for(repository, tmp_path / "baseline", commit), output_dir
    )
    hotspot_id = uuid4()
    round_authority = _round(hotspot_id)
    trusted_root = tmp_path / "trusted-input"
    replacements = {
        "noop": b"VALUE = (0)\n",
        "known_faster": b"VALUE = 1\n",
        "known_slower": b"VALUE = 2\n",
        "build_failure": b"VALUE = 3\n",
    }
    package_refs = {}
    candidate_ids = {}
    for fixture in SCRIPTED_CANDIDATE_FIXTURES:
        candidate_id = uuid4()
        candidate_hash = _candidate_hash(
            manager,
            baseline,
            candidate_id,
            output_dir,
            replacements[fixture.fixture_id],
        )
        manifest_hash, package_hash = _publish_package(
            trusted_root,
            candidate_id=candidate_id,
            hotspot_id=hotspot_id,
            baseline_hash=baseline.source_hash,
            candidate_hash=candidate_hash,
            replacement=replacements[fixture.fixture_id],
        )
        package_refs[fixture.fixture_id] = {
            "candidate_source_hash": candidate_hash,
            "source_package_hash": package_hash,
            "manifest_hash": manifest_hash,
            "manifest_schema_version": "m1-candidate-source-v1",
        }
        candidate_ids[fixture.fixture_id] = candidate_id

    packages = CandidateSourcePackageStore(
        trusted_root,
        profile=PROFILE,
        allowed_overlay_roots=("python/sglang",),
        approved_mount_targets={REPLACEMENT_POINT: MOUNT_TARGET},
    )
    intake = ScriptedCandidateIntake(
        packages, store_id=STORE_ID, store_hash=STORE_HASH
    )
    members = []
    for ordinal, fixture in enumerate(SCRIPTED_CANDIDATE_FIXTURES):
        resolved = intake.resolve(
            round_authority=round_authority,
            candidate_input=ScriptedCandidatePackageInput(
                ordinal=ordinal,
                source_package_ref=package_refs[fixture.fixture_id],
                optimization_intent=fixture.optimization_intent,
            ),
            baseline_source_hash=baseline.source_hash,
            profiler_evidence_uri=PROFILER_URI,
            profiler_evidence_hash=PROFILER_HASH,
        )
        assert resolved.member.candidate_id == candidate_ids[fixture.fixture_id]
        members.append(resolved.member)
    family_hash = candidate_family_hash(
        round_authority.model_dump(mode="json"),
        [member.model_dump(mode="json") for member in members],
    )
    building_round = SearchRound.model_validate(
        {
            **round_authority.model_dump(mode="json"),
            "state": "building",
            "candidate_family_hash": family_hash,
        }
    )
    builder = ScriptedRoundCandidateBuilder(
        manager,
        packages,
        LocalArtifactStore(output_dir / "artifacts", PROFILE),
        LocalBuildCache(output_dir / "build-cache"),
        profile=PROFILE,
        store_id=STORE_ID,
        store_hash=STORE_HASH,
    )
    return builder, building_round, members, baseline, output_dir, trusted_root


def test_four_scripted_fixtures_build_real_artifacts_and_failure_evidence(
    tmp_path: Path,
) -> None:
    builder, round_authority, members, baseline, output_dir, _ = _prepare_family(
        tmp_path
    )

    results = [
        builder.build_member(
            round_authority=round_authority,
            member=member,
            baseline=baseline,
            fixture_id=fixture.fixture_id,
            output_dir=output_dir,
        )
        for member, fixture in zip(members, SCRIPTED_CANDIDATE_FIXTURES, strict=True)
    ]

    assert [result.terminal.state.value for result in results] == [
        "built",
        "built",
        "built",
        "build_failed",
    ]
    for result in results[:3]:
        assert result.synthetic is True
        assert result.artifact is not None and result.artifact.synthetic is True
        assert result.artifact.kind == "python_overlay"
        assert result.artifact.metadata["performance_conclusion"] == "not_measured"
        assert file_uri_to_path(result.artifact.uri).is_file()
        assert result.failure_evidence_uri is None
    failed = results[-1]
    assert failed.artifact is None
    assert failed.terminal.terminal_failure_code == "scripted_build_failure"
    assert failed.failure_evidence_uri is not None
    assert file_uri_to_path(failed.failure_evidence_uri).is_file()
    assert not any((output_dir / "worktrees").iterdir())
    assert canonical_source_hash(file_uri_to_path(baseline.worktree_uri)) == baseline.source_hash


def test_scripted_build_replay_uses_same_artifact_identity_and_cleans_worktree(
    tmp_path: Path,
) -> None:
    builder, round_authority, members, baseline, output_dir, _ = _prepare_family(
        tmp_path
    )

    first = builder.build_member(
        round_authority=round_authority,
        member=members[0],
        baseline=baseline,
        fixture_id="noop",
        output_dir=output_dir,
    )
    replay = builder.build_member(
        round_authority=round_authority,
        member=members[0],
        baseline=baseline,
        fixture_id="noop",
        output_dir=output_dir,
    )

    assert replay.terminal == first.terminal
    assert replay.artifact == first.artifact
    assert not any((output_dir / "worktrees").iterdir())

    artifact_path = file_uri_to_path(first.artifact.uri)  # type: ignore[union-attr]
    artifact_path.chmod(0o644)
    try:
        with pytest.raises(ArtifactIntegrityError, match="content-addressed Store"):
            builder.build_member(
                round_authority=round_authority,
                member=members[0],
                baseline=baseline,
                fixture_id="noop",
                output_dir=output_dir,
            )
    finally:
        artifact_path.chmod(0o444)
    assert not any((output_dir / "worktrees").iterdir())


def test_scripted_build_rejects_store_drift_before_worktree_creation(
    tmp_path: Path,
) -> None:
    builder, round_authority, members, baseline, output_dir, trusted_root = (
        _prepare_family(tmp_path)
    )
    digest = members[0].candidate_source_hash.removeprefix("sha256:")
    manifest = trusted_root / "sha256" / digest[:2] / digest[2:] / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    with pytest.raises(ValidationError):
        builder.build_member(
            round_authority=round_authority,
            member=members[0],
            baseline=baseline,
            fixture_id="noop",
            output_dir=output_dir,
        )

    assert not any((output_dir / "worktrees").iterdir())


def test_scripted_build_rejects_unfrozen_round_and_unknown_fixture(
    tmp_path: Path,
) -> None:
    builder, round_authority, members, baseline, output_dir, _ = _prepare_family(
        tmp_path
    )

    with pytest.raises(SourceArtifactError, match="unknown M2a scripted Fixture"):
        builder.build_member(
            round_authority=round_authority,
            member=members[0],
            baseline=baseline,
            fixture_id="caller-controlled",
            output_dir=output_dir,
        )
    with pytest.raises(SourceArtifactError, match="frozen Candidate family"):
        builder.build_member(
            round_authority=SearchRound.model_validate(
                {
                    **round_authority.model_dump(mode="json"),
                    "state": "intake_open",
                    "candidate_family_hash": None,
                }
            ),
            member=members[0],
            baseline=baseline,
            fixture_id="noop",
            output_dir=output_dir,
        )

    assert not any((output_dir / "worktrees").iterdir())


def test_scripted_build_cleans_worktree_when_build_is_interrupted(
    tmp_path: Path,
) -> None:
    builder, round_authority, members, baseline, output_dir, _ = _prepare_family(
        tmp_path
    )

    class InterruptedCache:
        def lookup(self, cache_key: str):  # type: ignore[no-untyped-def]
            raise RuntimeError("scripted build interrupted")

        def record(self, cache_key: str, manifest) -> None:  # type: ignore[no-untyped-def]
            raise AssertionError("an interrupted build cannot populate the cache")

    builder.build_cache = InterruptedCache()
    with pytest.raises(RuntimeError, match="scripted build interrupted"):
        builder.build_member(
            round_authority=round_authority,
            member=members[0],
            baseline=baseline,
            fixture_id="noop",
            output_dir=output_dir,
        )

    assert not any((output_dir / "worktrees").iterdir())
    assert canonical_source_hash(file_uri_to_path(baseline.worktree_uri)) == baseline.source_hash
