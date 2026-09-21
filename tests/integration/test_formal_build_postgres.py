# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import hashlib
import os
from dataclasses import replace
from uuid import uuid4

import pytest

from hcuopt.adapters.formal_candidate_builder import FormalCandidateBuildResult
from hcuopt.contracts.m2 import RoundCandidateBuildTerminal
from hcuopt.contracts.platform_v1 import ArtifactManifest, SourceSnapshot
from hcuopt.contracts.v1 import ManualCandidateBuildResult
from hcuopt.domain.errors import Conflict
from hcuopt.storage import formal_build
from hcuopt.storage.formal_build import PostgresFormalBuildStore
from hcuopt.storage.formal_claim import PostgresFormalClaimStore
from tests.integration.test_formal_dispatch_postgres import dispatch_case  # noqa: F401
from tests.integration.test_formal_start_management_postgres import isolated_dsn  # noqa: F401
from tests.unit.f1c_helpers import git

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.getenv("HCUOPT_DATABASE_URL"), reason="requires PostgreSQL"),
]


@pytest.fixture
def build_record_case(dispatch_case, tmp_path):  # type: ignore[no-untyped-def] # noqa: F811
    dispatcher, intent_id = dispatch_case
    dispatcher.create(intent_id)
    claims = PostgresFormalClaimStore(dispatcher, enabled=True)
    claim = claims.claim(intent_id, "builder", ttl_seconds=300)
    repo = dispatcher.repository
    with repo.connection() as conn:
        member = conn.execute("SELECT * FROM round_candidates ORDER BY ordinal LIMIT 1").fetchone()
        round_ = conn.execute("SELECT * FROM search_rounds").fetchone()
        baseline = conn.execute("SELECT * FROM source_snapshots WHERE kind = 'baseline'").fetchone()
    # Real Git ancestry even when the build result itself is a database fixture.
    parent_path = tmp_path / "parent-git"
    parent_path.mkdir()
    git(parent_path, "init")
    git(parent_path, "config", "user.name", "Test Builder")
    git(parent_path, "config", "user.email", "builder@example.invalid")
    (parent_path / "source.py").write_text("BASELINE = True\n", encoding="utf-8")
    git(parent_path, "add", ".")
    git(parent_path, "commit", "-m", "baseline fixture")
    baseline_commit = git(parent_path, "rev-parse", "HEAD")
    (parent_path / "source.py").write_text("CANDIDATE = True\n", encoding="utf-8")
    git(parent_path, "commit", "-am", "candidate fixture")
    candidate_commit = git(parent_path, "rev-parse", "HEAD")
    candidate_tree = git(parent_path, "rev-parse", "HEAD^{tree}")
    git(parent_path, "checkout", "--detach", baseline_commit)
    with repo.connection() as conn:
        conn.execute(
            "UPDATE source_snapshots SET commit = %s, worktree_uri = %s WHERE snapshot_id = %s",
            (baseline_commit, parent_path.as_uri(), baseline["snapshot_id"]),
        )
    source = SourceSnapshot(
        snapshot_id=uuid4(), kind="candidate", repository=baseline["repository"],
        commit=candidate_commit, tree_hash=candidate_tree,
        source_hash=member["candidate_source_hash"], worktree_uri="fixture://removed-worktree",
        clean=True, parent_snapshot_id=baseline["snapshot_id"],
    )
    path = tmp_path / "overlay.py"
    path.write_bytes(b"# explicit database fixture\n")
    digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    path.chmod(0o444)
    artifact = ArtifactManifest(
        candidate_id=member["candidate_id"], kind="python_overlay", uri=path.as_uri(),
        content_hash=digest, source_snapshot_id=source.snapshot_id,
        metadata={
            "source_hash": source.source_hash, "read_only": True, "immutable": True,
            "overlay_files": [{"path": "overlay.py", "content_hash": digest}],
            "package_manifest_hash": member["source_manifest_hash"],
            "replacement_point": member["replacement_point"], "candidate_kind": "business",
        },
    )
    build = ManualCandidateBuildResult(
        candidate_id=member["candidate_id"], source=source, artifact=artifact,
        adapter_provenance=[{
            "profile": round_["adapter_profile"], "capability": capability,
            "adapter_name": "ExplicitDatabaseFixture", "adapter_version": "1",
            "implementation_kind": "real",
        } for capability in [
            "source_manager", "candidate_source_intake", "candidate_builder", "artifact_store"
        ]],
    )
    result = FormalCandidateBuildResult(build, RoundCandidateBuildTerminal(
        round_id=round_["round_id"], round_candidate_id=member["round_candidate_id"],
        candidate_id=member["candidate_id"], state="built",
        artifact_id=artifact.artifact_id, artifact_hash=digest,
    ))
    yield PostgresFormalBuildStore(claims, enabled=True), intent_id, claim["claim_token"], result
    path.chmod(0o666)


def test_atomic_success_and_exact_replay(build_record_case):  # type: ignore[no-untyped-def]
    store, intent_id, token, result = build_record_case
    first = store.record(intent_id, "builder", token, result)
    assert first["state"] == "built"
    assert store.record(intent_id, "builder", token, result) == first
    progress = store.claims.dispatcher.read_status(intent_id)
    candidate = next(c for c in progress.candidates if c.candidate_id == result.build.candidate_id)
    assert candidate.state == "built"
    assert candidate.artifact_id == result.build.artifact.artifact_id
    assert candidate.artifact_hash == result.build.artifact.content_hash
    changed_artifact = result.build.artifact.model_copy(update={"build_recipe": {"changed": True}})
    changed = replace(result, build=result.build.model_copy(update={"artifact": changed_artifact}))
    with pytest.raises(Conflict, match="another result"):
        store.record(intent_id, "builder", token, changed)
    with store.claims.dispatcher.repository.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM artifacts").fetchone()["n"] == 1
        assert conn.execute(
            "SELECT count(*) AS n FROM task_events "
            "WHERE event_type = 'formal_candidate_build_recorded'"
        ).fetchone()["n"] == 1


def test_event_failure_rolls_back_all_build_writes(build_record_case, monkeypatch):  # type: ignore[no-untyped-def]
    store, intent_id, token, result = build_record_case
    original = formal_build._insert

    def fail(conn, table, payload):  # type: ignore[no-untyped-def]
        original(conn, table, payload)
        if table == "task_events":
            raise RuntimeError("injected audit failure")

    monkeypatch.setattr(formal_build, "_insert", fail)
    with pytest.raises(RuntimeError, match="injected"):
        store.record(intent_id, "builder", token, result)
    with store.claims.dispatcher.repository.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM artifacts").fetchone()["n"] == 0
        assert conn.execute(
            "SELECT count(*) AS n FROM source_snapshots WHERE kind = 'candidate'"
        ).fetchone()["n"] == 0
        row = conn.execute("SELECT state FROM search_rounds").fetchone()
        assert row["state"] == "intake_closed"


def test_stop_refuses_build_publication(build_record_case):  # type: ignore[no-untyped-def]
    store, intent_id, token, result = build_record_case
    store.claims.request_stop(intent_id, requested_by="operator")
    with pytest.raises(Conflict, match="stop requested"):
        store.record(intent_id, "builder", token, result)


def test_invalid_parent_and_tampered_file_cannot_publish(build_record_case):  # type: ignore[no-untyped-def]
    from hcuopt.source_hash import file_uri_to_path

    store, intent_id, token, result = build_record_case
    source = result.build.source.model_copy(update={"parent_snapshot_id": uuid4()})
    changed = replace(result, build=result.build.model_copy(update={"source": source}))
    with pytest.raises(Conflict, match="durable parent"):
        store.record(intent_id, "builder", token, changed)
    path = file_uri_to_path(result.build.artifact.uri)
    path.chmod(0o666)
    try:
        with pytest.raises(Conflict, match="read-only"):
            store.record(intent_id, "builder", token, result)
        path.write_bytes(b"tampered")
    finally:
        path.chmod(0o444)
    with pytest.raises(Conflict, match="content Hash"):
        store.record(intent_id, "builder", token, result)


@pytest.mark.parametrize("fault", ["baseline_commit", "wrong_tree"])
def test_git_ancestry_must_match_real_candidate(build_record_case, fault):
    store, intent_id, token, result = build_record_case
    with store.claims.dispatcher.repository.connection() as conn:
        baseline = conn.execute(
            "SELECT commit FROM source_snapshots WHERE kind = 'baseline'"
        ).fetchone()
    changes = {"commit": baseline["commit"]} if fault == "baseline_commit" else {
        "tree_hash": "f" * 40,
    }
    changed = replace(result, build=result.build.model_copy(update={
        "source": result.build.source.model_copy(update=changes),
    }))
    with pytest.raises(Conflict, match="Git parent or tree"):
        store.record(intent_id, "builder", token, changed)
