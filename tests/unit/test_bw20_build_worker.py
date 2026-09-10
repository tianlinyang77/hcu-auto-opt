# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from pathlib import Path
from uuid import uuid4

import pytest

from hcuopt.deployment import bw20_build_worker as subject
from hcuopt.targets import load_target

ROOT = Path(__file__).parents[2]


@pytest.fixture
def target():
    return load_target(ROOT / "config/targets/bw20-sglang-0.5.12.yaml")


def test_root_is_bound_to_task_id(tmp_path, monkeypatch):
    monkeypatch.setattr(subject, "PARENT", tmp_path)
    task_id = str(uuid4())
    root = tmp_path / task_id
    root.mkdir()
    assert subject.validate_root(root, task_id) == root
    with pytest.raises(ValueError, match="canonical"):
        subject.validate_root(root, str(uuid4()))


@pytest.mark.parametrize("field,value", [
    ("clean_checkout", "/tmp/untrusted"), ("commit", "a" * 40),
])
def test_source_override_rejected(target, field, value):
    setattr(target.source_baseline, field, value)
    with pytest.raises(ValueError, match="shared source"):
        subject.validate_target(target)


@pytest.mark.parametrize("job_type", ["framework_smoke", "stage0_probe", "shell", ""])
def test_no_gpu_or_arbitrary_actions(job_type, tmp_path):
    with pytest.raises(ValueError, match="only"):
        subject.run_job({"job_type": job_type}, tmp_path)


def test_original_source_payload_does_not_require_invented_fingerprint(
        target, tmp_path, monkeypatch):
    monkeypatch.setattr(subject, "PARENT", tmp_path)
    task_id, job_id = str(uuid4()), str(uuid4())
    root = tmp_path / task_id
    root.mkdir()
    payload = {"task_id": task_id, "target": target.model_dump(mode="json"),
               "target_snapshot_id": str(uuid4())}
    values = {"rev-parse": target.source_baseline.commit, "status": "",
              "config": target.source_baseline.repository}
    monkeypatch.setattr(subject, "git", lambda path, command, *args: values[command])
    sentinel = {"source": "mock-only-contract-routing-test", "synthetic": True}
    calls = []

    def handle(self, job_type, payload):
        calls.append((job_type, payload))
        return sentinel

    monkeypatch.setattr(subject.JobHandlers, "handle", handle)
    record = subject.run_job({"job_type": "source_prepare", "job_id": job_id,
                              "payload": payload}, root)
    assert calls == [("source_prepare", payload)]
    assert record["result"] is sentinel
    assert record["shared_before"] == record["shared_after"]
    assert (root / f"job-{job_id}.json").is_file()
    with pytest.raises(ValueError, match="receipt already exists"):
        subject.run_job({"job_type": "source_prepare", "job_id": job_id,
                          "payload": payload}, root)


def test_foreign_baseline_never_reaches_git(target, tmp_path):
    from hcuopt.contracts.platform_v1 import SourceSnapshot
    manager = subject.IsolatedBW20SourceManager(tmp_path)
    baseline = SourceSnapshot(repository=target.source_baseline.repository,
        commit=target.source_baseline.commit, worktree_uri=(tmp_path / "foreign").as_uri(),
        source_hash="sha256:" + "0" * 64, tree_hash="a" * 40, kind="baseline", clean=True)
    with pytest.raises(ValueError, match="outside this task"):
        manager.create_candidate(baseline, uuid4(), tmp_path / "work")
