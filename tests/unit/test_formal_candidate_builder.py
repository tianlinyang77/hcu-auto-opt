# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json

import pytest

from hcuopt.adapters.formal_candidate_builder import FormalRoundCandidateBuilder
from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.m2_candidate import candidate_source_package_hash
from hcuopt.domain.enums import (
    ManualCandidateKind,
    RoundCandidateState,
    RoundPhase,
    SearchRoundState,
)
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path
from tests.unit import test_m1_candidate_builder as local
from tests.unit import test_m2_formal_execution as fixture


@pytest.fixture
def build_case(tmp_path):  # type: ignore[no-untyped-def]
    # This wrapper test needs ordinary source files, not the shared symlink fixture.
    # Existing source-manager symlink tests remain unchanged.
    repository = tmp_path / "origin"
    repository.mkdir()
    local.git(repository, "init")
    local.git(repository, "config", "user.name", "Test Builder")
    local.git(repository, "config", "user.email", "builder@example.invalid")
    source_file = repository / local.REPLACEMENT_PATH
    source_file.parent.mkdir(parents=True)
    source_file.write_text("MARKER = 'baseline'\n", encoding="utf-8")
    local.git(repository, "add", ".")
    local.git(repository, "commit", "-m", "test fixture")
    commit = local.git(repository, "rev-parse", "HEAD")
    output = tmp_path / "run"
    manager = GitSourceManager(local.PROFILE)
    baseline = manager.prepare_baseline(
        local.target_for(repository, tmp_path / "baseline", commit), output
    )
    round_ = fixture._round(RoundPhase.SEARCH).model_copy(update={
        "state": SearchRoundState.INTAKE_CLOSED, "artifact_family_hash": None,
        "adapter_profile": local.PROFILE, "replacement_point": "sglang.triton_kernel",
    })
    member = fixture._member(round_, RoundPhase.SEARCH)
    replacement = b"MARKER = 'formal-business-test'\n"
    source_hash = local._candidate_hash(manager, baseline, member.candidate_id, output, replacement)
    root = tmp_path / "packages"
    manifest = local._publish_source_package(
        root, candidate_id=member.candidate_id, hotspot_id=round_.hotspot_id,
        baseline_hash=baseline.source_hash, candidate_hash=source_hash, replacement=replacement,
    )
    # Explicit test source package, not a production reviewed candidate.
    manifest = manifest.model_copy(update={"candidate_kind": ManualCandidateKind.BUSINESS})
    digest = source_hash.removeprefix("sha256:")
    manifest_path = root / "sha256" / digest[:2] / digest[2:] / "manifest.json"
    manifest_path.write_text(json.dumps(manifest.model_dump(mode="json")), encoding="utf-8")
    builder = local._builder(manager, root, output)
    package = builder.source_packages.read(candidate_source_hash=source_hash)
    member = member.model_copy(update={
        "state": RoundCandidateState.INTAKE_ACCEPTED, "artifact_id": None, "artifact_hash": None,
        "baseline_source_hash": baseline.source_hash, "candidate_source_hash": source_hash,
        "candidate_kind": ManualCandidateKind.BUSINESS,
        "replacement_point": round_.replacement_point,
        "source_manifest_hash": package.manifest_hash,
        "source_package_hash": candidate_source_package_hash(package.manifest_hash, manifest.files),
    })
    formal = FormalRoundCandidateBuilder(
        builder, store_id=member.source_package_store_id,
        store_hash=member.source_package_store_hash, enabled=True,
    )
    kwargs = dict(
        round_authority=round_, member=member, baseline=baseline,
        hotspot={"profiler_raw_output_uri": local.PROFILER_URI,
                 "profiler_raw_output_hash": local.PROFILER_HASH},
        hotspot_intake_hash=local.HOTSPOT_INTAKE_HASH, output_dir=output,
    )
    return formal, kwargs, replacement


def test_real_overlay_build_and_cache_preserve_baseline(build_case):  # type: ignore[no-untyped-def]
    builder, kwargs, replacement = build_case
    first = builder.build_member(**kwargs)
    second = builder.build_member(**kwargs)
    assert first.build.artifact == second.build.artifact
    assert first.terminal == second.terminal
    assert first.terminal.artifact_hash == first.build.artifact.content_hash
    assert first.build.artifact.synthetic is False
    path = file_uri_to_path(first.build.artifact.uri)
    assert path.read_bytes() == replacement
    assert path.stat().st_mode & 0o222 == 0
    assert not any((kwargs["output_dir"] / "worktrees").iterdir())
    baseline = kwargs["baseline"]
    assert canonical_source_hash(file_uri_to_path(baseline.worktree_uri)) == baseline.source_hash


@pytest.mark.parametrize("fault", ["disabled", "fixture", "manifest", "store", "profile", "state"])
def test_reject_before_worktree_or_artifact(build_case, fault):  # type: ignore[no-untyped-def]
    builder, kwargs, _ = build_case
    if fault == "disabled":
        builder.enabled = False
    elif fault == "profile":
        kwargs["round_authority"] = kwargs["round_authority"].model_copy(
            update={"adapter_profile": "wrong-profile"}
        )
    else:
        changes = {
            "fixture": {"candidate_kind": ManualCandidateKind.FIXTURE},
            "manifest": {"source_manifest_hash": "sha256:" + "0" * 64},
            "store": {"source_package_store_id": "another-store"},
            "state": {"state": RoundCandidateState.CORRECTNESS_PASSED},
        }
        kwargs["member"] = kwargs["member"].model_copy(update=changes[fault])
    with pytest.raises(SourceArtifactError):
        builder.build_member(**kwargs)
    assert not any((kwargs["output_dir"] / "worktrees").iterdir())


def test_source_apply_failure_cleans_worktree(build_case, monkeypatch):  # type: ignore[no-untyped-def]
    builder, kwargs, _ = build_case

    def fail(*args):  # type: ignore[no-untyped-def]
        raise SourceArtifactError("injected source apply failure")

    monkeypatch.setattr(builder.builder.source_packages, "apply", fail)
    with pytest.raises(SourceArtifactError, match="injected"):
        builder.build_member(**kwargs)
    assert not any((kwargs["output_dir"] / "worktrees").iterdir())
