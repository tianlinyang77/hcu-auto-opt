from __future__ import annotations

import tarfile
from pathlib import Path
from uuid import uuid4

import pytest
from f1c_helpers import create_repository, target_for

from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.noop_builder import NOOP_BUILD_RECIPE_VERSION, NoopBuilder
from hcuopt.domain.errors import SourceIntegrityError
from hcuopt.source_hash import file_uri_to_path


def _candidate(tmp_path: Path):  # type: ignore[no-untyped-def]
    repository, commit = create_repository(tmp_path / "origin")
    output_dir = tmp_path / "run"
    manager = GitSourceManager()
    baseline = manager.prepare_baseline(
        target_for(repository, tmp_path / "baseline", commit), output_dir
    )
    candidate_id = uuid4()
    candidate = manager.create_candidate(baseline, candidate_id, output_dir)
    payload = {
        "candidate_id": str(candidate_id),
        "source_snapshot": candidate.model_dump(mode="json"),
        "adapter_provenance": [manager.provenance.model_dump(mode="json")],
    }
    return manager, baseline, candidate, payload


def test_same_input_produces_same_artifact_hash(tmp_path: Path) -> None:
    _, _, candidate, payload = _candidate(tmp_path)
    builder = NoopBuilder()

    first = builder.build(payload, tmp_path / "build-one")
    second = builder.build(payload, tmp_path / "build-two")

    assert first.content_hash == second.content_hash
    assert first.build_recipe["name"] == NOOP_BUILD_RECIPE_VERSION
    assert first.source_snapshot_id == candidate.snapshot_id
    assert first.synthetic is False
    with tarfile.open(file_uri_to_path(first.uri), "r") as archive:
        assert ".git" not in archive.getnames()
        assert "README.md" in archive.getnames()


def test_builder_rejects_source_changed_after_snapshot(tmp_path: Path) -> None:
    _, _, candidate, payload = _candidate(tmp_path)
    (file_uri_to_path(candidate.worktree_uri) / "README.md").write_text(
        "changed\n", encoding="utf-8"
    )

    with pytest.raises(SourceIntegrityError, match="no longer matches"):
        NoopBuilder().build(payload, tmp_path / "build")


def test_cache_key_is_stable_and_binds_builder_version(tmp_path: Path) -> None:
    _, _, candidate, _ = _candidate(tmp_path)
    builder = NoopBuilder()
    key = builder.cache_key(candidate, builder.provenance)

    assert builder.cache_key(candidate, builder.provenance) == key
    changed = builder.provenance.model_copy(update={"adapter_version": "2.0.0"})
    assert builder.cache_key(candidate, changed) != key
