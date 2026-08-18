from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from f1c_helpers import create_repository, git, target_for

from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.domain.errors import SourceIntegrityError
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path


def test_locked_commit_wins_over_a_moving_branch(tmp_path: Path) -> None:
    repository, locked_commit = create_repository(tmp_path / "origin")
    (repository / "README.md").write_text("moving branch\n", encoding="utf-8")
    git(repository, "add", "README.md")
    git(repository, "commit", "-m", "move branch")
    assert git(repository, "rev-parse", "HEAD") != locked_commit

    target = target_for(repository, tmp_path / "baseline", locked_commit)
    manager = GitSourceManager()
    snapshot = manager.prepare_baseline(target, tmp_path / "run")

    assert snapshot.commit == locked_commit
    assert snapshot.clean is True
    assert file_uri_to_path(snapshot.worktree_uri) == (tmp_path / "baseline").resolve()
    assert (tmp_path / "baseline" / "README.md").read_text() == "baseline\n"


def test_candidate_is_isolated_and_cleanup_preserves_baseline(tmp_path: Path) -> None:
    repository, commit = create_repository(tmp_path / "origin")
    target = target_for(repository, tmp_path / "baseline", commit)
    output_dir = tmp_path / "run"
    manager = GitSourceManager()
    baseline = manager.prepare_baseline(target, output_dir)
    candidate = manager.create_candidate(baseline, uuid4(), output_dir)

    candidate_path = file_uri_to_path(candidate.worktree_uri)
    baseline_path = file_uri_to_path(baseline.worktree_uri)
    assert candidate_path != baseline_path
    assert candidate.source_hash == baseline.source_hash
    assert candidate.tree_hash == baseline.tree_hash

    (candidate_path / "README.md").write_text("candidate-only\n", encoding="utf-8")
    assert (baseline_path / "README.md").read_text() == "baseline\n"
    manager.remove_candidate(baseline, candidate, output_dir)

    assert not candidate_path.exists()
    assert canonical_source_hash(baseline_path) == baseline.source_hash
    assert git(baseline_path, "status", "--porcelain=v1") == ""


def test_dirty_baseline_is_rejected(tmp_path: Path) -> None:
    repository, commit = create_repository(tmp_path / "origin")
    target = target_for(repository, tmp_path / "baseline", commit)
    manager = GitSourceManager()
    manager.prepare_baseline(target, tmp_path / "run")
    (tmp_path / "baseline" / "README.md").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(SourceIntegrityError, match="not clean"):
        manager.prepare_baseline(target, tmp_path / "run")


def test_output_inside_baseline_is_rejected_before_clone(tmp_path: Path) -> None:
    repository, commit = create_repository(tmp_path / "origin")
    baseline = tmp_path / "baseline"
    target = target_for(repository, baseline, commit)

    with pytest.raises(SourceIntegrityError, match="cannot be inside"):
        GitSourceManager().prepare_baseline(target, baseline / "results")

    assert not baseline.exists()


def test_recovery_only_removes_uuid_named_managed_worktrees(tmp_path: Path) -> None:
    repository, commit = create_repository(tmp_path / "origin")
    target = target_for(repository, tmp_path / "baseline", commit)
    output_dir = tmp_path / "run"
    manager = GitSourceManager()
    baseline = manager.prepare_baseline(target, output_dir)
    candidate = manager.create_candidate(baseline, uuid4(), output_dir)
    unmanaged = output_dir / "worktrees" / "keep-me"
    unmanaged.mkdir()

    recovered = manager.recover_candidates(baseline, output_dir)

    assert recovered == (candidate.worktree_uri,)
    assert not file_uri_to_path(candidate.worktree_uri).exists()
    assert unmanaged.is_dir()


def test_source_hash_ignores_location_and_timestamp_but_tracks_executable_bit(
    tmp_path: Path,
) -> None:
    first, _ = create_repository(tmp_path / "first")
    second = tmp_path / "second"
    target = target_for(first, second, git(first, "rev-parse", "HEAD"))
    GitSourceManager().prepare_baseline(target, tmp_path / "run")

    first_hash = canonical_source_hash(first)
    assert canonical_source_hash(second) == first_hash
    (second / "run.sh").chmod(0o644)
    assert canonical_source_hash(second) != first_hash
