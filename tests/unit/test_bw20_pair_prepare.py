# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Real local Builder/Store on a small Git fixture; never BW20 hardware evidence."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from hcuopt.adapters.bw20_execution import BW20SmokeExecutionAdapter
from hcuopt.adapters.execution import LocalCommandRunner
from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.local_artifact_store import LocalArtifactStore
from hcuopt.adapters.noop_builder import NoopBuilder
from hcuopt.contracts.platform_v1 import ExecutionRequest
from hcuopt.deployment.bw20_pair_prepare import prepare_pair, verify_prepared_pair
from hcuopt.deployment.kernel_binary_inventory import file_hash
from hcuopt.evaluation.sglang_smoke import load_workload_spec
from hcuopt.source_hash import file_uri_to_path
from hcuopt.targets import load_target
from tests.unit.f1c_helpers import git, target_for
from tests.unit.test_execution_adapters import ScriptedDockerRunner

REPO = Path(__file__).parents[2]


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    def forbid_execution(*args, **kwargs):
        raise AssertionError("offline preparation must not invoke a command runner")
    monkeypatch.setattr(LocalCommandRunner, "run", forbid_execution)
    monkeypatch.setattr(LocalCommandRunner, "popen", forbid_execution)
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(origin)], check=True, capture_output=True)
    git(origin, "config", "user.name", "BW20 fixture")
    git(origin, "config", "user.email", "bw20-fixture@example.invalid")
    (origin / "README.md").write_text("local fixture only\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "test: local source fixture")
    manager = GitSourceManager()
    baseline = manager.prepare_baseline(
        target_for(origin, tmp_path / "baseline", git(origin, "rev-parse", "HEAD")),
        tmp_path / "source-run")
    candidate_id = uuid4()
    snapshot = manager.create_candidate(baseline, candidate_id, tmp_path / "source-run")
    original_snapshot = snapshot.model_copy(deep=True)
    payload = {"candidate_id": str(candidate_id), "source_snapshot": snapshot,
               "adapter_provenance": [manager.provenance.model_dump(mode="json")]}
    built = NoopBuilder().build(payload, tmp_path / "build")
    store = LocalArtifactStore(tmp_path / "store")
    if os.name == "nt":
        # Windows cannot unlink the publisher's readonly temporary hardlink.
        # Explicitly exercise verified EXISTING CAS reuse here, not atomic publication.
        digest = built.content_hash.removeprefix("sha256:")
        cached = store.root / "sha256" / digest[:2] / digest[2:]
        cached.parent.mkdir(parents=True)
        shutil.copyfile(file_uri_to_path(built.uri), cached)
        cached.chmod(0o444)
    stored = store.publish(built, file_uri_to_path(built.uri))
    target = load_target(REPO / "config/targets/bw20-sglang-0.5.12.yaml")
    # Explicit fixture source lock, NOT an assertion about the remote SGLang checkout.
    target.source_baseline.repository = snapshot.repository
    target.source_baseline.commit = snapshot.commit
    runner_path = REPO / "src/hcuopt/evaluation/sglang_smoke_runner.py"
    values = dict(
        target=target, artifact=stored, snapshot=snapshot,
        workload=load_workload_spec(REPO / "config/workloads/bw20-sglang-smoke-v1.yaml"),
        runner_path=runner_path, expected_runner_sha256=file_hash(runner_path),
        destination=tmp_path / "prepared", fencing_token=1,
    )
    yield values
    manager.remove_candidate(baseline, original_snapshot, tmp_path / "source-run")
    assert file_uri_to_path(baseline.worktree_uri).exists()


def test_real_builder_store_pair_preparation_and_existing_executor(inputs, tmp_path):
    original = inputs["artifact"].model_dump(mode="json")
    prepared = prepare_pair(**inputs)
    result = verify_prepared_pair(prepared.directory, expected_plan_sha256=prepared.plan_sha256)
    assert result["local_bundle_verified"] is True
    assert result["execution_performed"] is False
    plan = json.loads((prepared.directory / "plan.json").read_text())
    assert plan["original_artifact"] == original
    staged = plan["staged_artifact"]
    assert staged["artifact_id"] == original["artifact_id"]
    assert staged["content_hash"] == original["content_hash"]
    assert staged["uri"] != original["uri"]
    assert staged["metadata"]["staged_from_uri"] == original["uri"]
    assert file_hash(prepared.directory / "input/noop-source.tar") == original["content_hash"]
    requests = [ExecutionRequest.model_validate(r) for r in plan["requests"]]
    assert requests[0].request_id != requests[1].request_id
    assert requests[0].argv == requests[1].argv
    assert requests[0].environment == requests[1].environment
    assert len(requests[1].mounts) == len(requests[0].mounts) + 1
    runner = ScriptedDockerRunner()
    adapter = BW20SmokeExecutionAdapter(runner, run_root=plan["remote_run_root"])
    for request in requests:
        adapter.validate_scope(request, inputs["target"])
    assert runner.commands == []
    # An explicit fake Docker runner verifies the lifecycle connection only.
    for request in requests:
        executed = adapter.execute(request, inputs["target"], tmp_path / "fake-execution")
        assert executed.status == "succeeded"
        assert executed.metadata["automatic_release_allowed"] is False
    assert plan["remote_files_verified"] is False
    assert plan["candidate_activation"] == "archive_mount_only"
    assert inputs["artifact"].model_dump(mode="json") == original
    cli = subprocess.run(
        [sys.executable, "-m", "hcuopt.deployment.bw20_pair_prepare", "verify",
         "--directory", str(prepared.directory), "--plan-sha256", prepared.plan_sha256],
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
        check=True, capture_output=True, text=True,
    )
    assert json.loads(cli.stdout)["local_bundle_verified"] is True


@pytest.mark.parametrize("change", ["hash", "synthetic", "snapshot", "commit", "repository",
                                    "dirty", "kind", "unpublished", "provenance", "runner"])
def test_invalid_artifact_or_source_never_creates_bundle(inputs, change):
    if change == "hash":
        inputs["artifact"].content_hash = "sha256:" + "0" * 64
    elif change == "synthetic":
        inputs["artifact"].synthetic = True
    elif change == "snapshot":
        inputs["artifact"].source_snapshot_id = uuid4()
    elif change == "commit":
        inputs["snapshot"].commit = "a" * 40
    elif change == "repository":
        inputs["snapshot"].repository = "different-repository"
    elif change == "dirty":
        inputs["snapshot"].clean = False
    elif change == "kind":
        inputs["artifact"].kind = "binary-overlay"
    elif change == "unpublished":
        inputs["artifact"].metadata["immutable"] = False
    elif change == "provenance":
        inputs["artifact"].metadata["adapter_provenance"] = []
    else:
        inputs["expected_runner_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError):
        prepare_pair(**inputs)
    assert not inputs["destination"].exists()


@pytest.mark.parametrize("file", ["spec.json", "noop-source.tar", "sglang_smoke_runner.py"])
def test_detects_staged_byte_tampering(inputs, file):
    prepared = prepare_pair(**inputs)
    path = prepared.directory / "input" / file
    path.chmod(0o644)
    path.write_bytes(b"tampered fixture")
    with pytest.raises(ValueError, match="staged hash mismatch"):
        verify_prepared_pair(prepared.directory, expected_plan_sha256=prepared.plan_sha256)


def test_plan_mutation_requires_external_trusted_digest(inputs):
    prepared = prepare_pair(**inputs)
    path = prepared.directory / "plan.json"
    plan = json.loads(path.read_text())
    plan["requests"][0]["environment"]["LD_PRELOAD"] = "/tmp/evil.so"
    path.write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="plan hash mismatch"):
        verify_prepared_pair(prepared.directory, expected_plan_sha256=prepared.plan_sha256)


@pytest.mark.parametrize("extra", ["input/extra.py", "baseline/old.log", "noop/old.log"])
def test_reject_extra_inputs_or_old_output(inputs, extra):
    prepared = prepare_pair(**inputs)
    (prepared.directory / extra).write_text("fixture")
    with pytest.raises(ValueError):
        verify_prepared_pair(prepared.directory, expected_plan_sha256=prepared.plan_sha256)


def test_refuses_overwriting_existing_bundle(inputs):
    prepared = prepare_pair(**inputs)
    digest = file_hash(prepared.directory / "plan.json")
    with pytest.raises(FileExistsError):
        prepare_pair(**inputs)
    assert file_hash(prepared.directory / "plan.json") == digest


@pytest.mark.parametrize("key", ["runner_path", "artifact"])
def test_reject_symlink_input(inputs, tmp_path, key):
    source = inputs["runner_path"] if key == "runner_path" else file_uri_to_path(inputs[key].uri)
    link = tmp_path / "redirected"
    try:
        link.symlink_to(source)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege unavailable")
        raise
    if key == "runner_path":
        inputs[key] = link
    else:
        inputs[key].uri = link.as_uri()
    with pytest.raises(ValueError, match="redirected"):
        prepare_pair(**inputs)


def test_reject_workload_and_lease_drift(inputs):
    inputs["workload"].tensor_parallel_size = 2
    with pytest.raises(ValueError, match="bounded"):
        prepare_pair(**inputs)
    inputs["workload"].tensor_parallel_size = 1
    inputs["fencing_token"] = 0
    with pytest.raises(ValueError):
        prepare_pair(**inputs)
    assert not inputs["destination"].exists()


def test_transport_preserves_published_uri_and_bound_request_ids(inputs, tmp_path):
    transport = tmp_path / "downloaded.tar"
    shutil.copyfile(file_uri_to_path(inputs["artifact"].uri), transport)
    published_uri = "file:///remote/immutable/source.tar"
    inputs["artifact"].uri = published_uri
    ids = (uuid4(), uuid4())
    prepared = prepare_pair(**inputs, artifact_transport_path=transport, request_ids=ids)
    plan = json.loads((prepared.directory / "plan.json").read_text())
    assert plan["original_artifact"]["uri"] == published_uri
    assert plan["staged_artifact"]["metadata"]["staged_from_uri"] == published_uri
    assert [r["request_id"] for r in plan["requests"]] == list(map(str, ids))
    assert inputs["artifact"].uri == published_uri


def test_transport_hash_mismatch_rejected(inputs, tmp_path):
    transport = tmp_path / "wrong.tar"
    transport.write_bytes(b"not the published bytes")
    with pytest.raises(ValueError, match="artifact hash mismatch"):
        prepare_pair(**inputs, artifact_transport_path=transport)
    assert not inputs["destination"].exists()


def test_duplicate_request_ids_rejected_before_staging(inputs):
    same = uuid4()
    with pytest.raises(ValueError, match="IDs must differ"):
        prepare_pair(**inputs, request_ids=(same, same))
    assert not inputs["destination"].exists()
