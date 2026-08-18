from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from f1c_helpers import create_repository, git, target_for

from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.local_artifact_store import LocalArtifactStore
from hcuopt.adapters.noop_builder import NoopBuilder
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path
from hcuopt.workers.handlers import JobHandlers


def test_real_f1c_noop_pipeline_leaves_a_traceable_clean_chain(tmp_path: Path) -> None:
    profile = "f1-c-test-v1"
    repository, commit = create_repository(tmp_path / "origin")
    output_dir = tmp_path / "run"
    manager = GitSourceManager(profile)
    builder = NoopBuilder(profile)
    store = LocalArtifactStore(output_dir / "artifacts", profile)

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

    built_once = builder.build(payload, output_dir)
    built_twice = builder.build(payload, output_dir)
    published = store.publish(built_once, file_uri_to_path(built_once.uri))
    manager.remove_candidate(baseline, candidate, output_dir)

    capabilities = {
        item["capability"] for item in published.metadata["adapter_provenance"]
    }
    assert capabilities == {"source_manager", "builder", "artifact_store"}
    assert built_once.content_hash == built_twice.content_hash
    assert file_uri_to_path(published.uri).is_file()
    assert not file_uri_to_path(candidate.worktree_uri).exists()
    baseline_path = file_uri_to_path(baseline.worktree_uri)
    assert canonical_source_hash(baseline_path) == baseline.source_hash
    assert git(baseline_path, "status", "--porcelain=v1") == ""


def test_real_f1c_adapters_integrate_with_framework_smoke_handlers(tmp_path: Path) -> None:
    profile = "f1-c-handler-test-v1"
    repository, commit = create_repository(tmp_path / "origin")
    target = target_for(repository, tmp_path / "baseline", commit)
    output_dir = tmp_path / "run"
    registry = AdapterRegistry(
        profile=profile,
        source_manager=GitSourceManager(profile),
        builder=NoopBuilder(profile),
        artifact_store=LocalArtifactStore(output_dir / "artifacts", profile),
    )
    handlers = JobHandlers(registry, output_dir)

    prepared = handlers.handle_source_prepare({"target": target.model_dump(mode="json")})
    built = handlers.handle_noop_build(
        {
            "baseline_source": prepared["source"],
            "candidate_id": str(uuid4()),
        }
    )

    assert built["synthetic"] is False
    assert built["artifact"]["content_hash"].startswith("sha256:")
    assert file_uri_to_path(built["artifact"]["uri"]).is_file()
    assert not any((output_dir / "worktrees").iterdir())


def test_handler_cleans_candidate_when_artifact_publication_fails(tmp_path: Path) -> None:
    profile = "f1-c-cleanup-test-v1"
    repository, commit = create_repository(tmp_path / "origin")
    target = target_for(repository, tmp_path / "baseline", commit)
    output_dir = tmp_path / "run"
    store = LocalArtifactStore(output_dir / "artifacts", profile)

    class FailingStore:
        provenance = store.provenance

        def publish(self, manifest, source_path):  # type: ignore[no-untyped-def]
            raise RuntimeError("injected publish failure")

    handlers = JobHandlers(
        AdapterRegistry(
            profile=profile,
            source_manager=GitSourceManager(profile),
            builder=NoopBuilder(profile),
            artifact_store=FailingStore(),
        ),
        output_dir,
    )
    prepared = handlers.handle_source_prepare({"target": target.model_dump(mode="json")})

    with pytest.raises(RuntimeError, match="injected publish failure"):
        handlers.handle_noop_build(
            {
                "baseline_source": prepared["source"],
                "candidate_id": str(uuid4()),
            }
        )

    assert not any((output_dir / "worktrees").iterdir())
