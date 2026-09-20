# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import ast
import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from hcuopt.deployment.bw20_stage0_guards import (
    VERIFY_SOURCE,
    BW20SourceGuard,
    guarded_job_session,
)
from tests.unit.test_bw20_stage0_runtime import plan


def digest(data):
    return hashlib.sha256(data).hexdigest()


def staged(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "worker.py").write_bytes(b"# frozen controller\n")
    raw = json.dumps({"src/worker.py": digest((source / "worker.py").read_bytes())}).encode()
    (tmp_path / "controller-manifest.json").write_bytes(raw)
    return "sha256:" + digest(raw)


@pytest.mark.parametrize("damage", [None, "changed", "extra", "missing", "pin", "empty_dir"])
def test_real_readonly_verifier_program(tmp_path, damage):
    pin = staged(tmp_path)
    if damage == "changed":
        (tmp_path / "src/worker.py").write_text("changed")
    elif damage == "extra":
        (tmp_path / "src/extra.py").write_text("unexpected")
    elif damage == "missing":
        (tmp_path / "src/worker.py").unlink()
    elif damage == "pin":
        pin = "sha256:" + "0" * 64
    elif damage == "empty_dir":
        (tmp_path / "extra").mkdir()
    result = subprocess.run([sys.executable, "-I", "-S", "-c", VERIFY_SOURCE,
                             str(tmp_path), pin], capture_output=True, timeout=5)
    assert (result.returncode == 0) is (damage is None), result.stderr
    if damage is None:
        assert json.loads(result.stdout)["file_count"] == 1
    ast.parse(VERIFY_SOURCE, feature_version=(3, 6))


def test_bound_source_guard_uses_bytes_and_rejects_another_plan():
    expected = plan()
    pin = "sha256:" + "a" * 64
    calls = []

    class Runner:
        host, user, port = "10.17.1.20", "github", 22

        def run(self, argv, timeout):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stderr=b"", stdout=json.dumps(dict(
                schema_version="bw20-controller-check-v1", root=expected.source_root,
                manifest_sha256=pin, file_count=1, total_bytes=20)).encode())

    guard = BW20SourceGuard(runner=Runner(), source_root=expected.source_root,
                           manifest_sha256=pin)
    guard(expected)
    assert len(guard.observations) == 1 and calls[0][0] == "python3"
    from dataclasses import replace
    with pytest.raises(ValueError):
        guard(replace(expected, source_root=expected.source_root + "-other"))
    assert len(calls) == 1


@pytest.mark.parametrize("field,value", [
    ("assert_live_lease", True), ("assert_live_lease", None),
    ("fencing_token", True), ("resource_id", "hcu-7"), ("lease_scope", "shared"),
])
def test_job_bridge_rejects_untrusted_context(field, value):
    p = plan()
    context = dict(job_id=str(uuid4()), lease_id=str(uuid4()), lease_scope="exclusive",
                   resource_id=p.resource_id, fencing_token=p.fencing_token,
                   assert_live_lease=lambda: None)
    context[field] = value
    with pytest.raises(ValueError):
        guarded_job_session(plan=p, transport=None, source_guard=None, context=context,
                            cancelled=lambda: False)


@pytest.mark.parametrize("denied", [None, "lease", "source"])
def test_guarded_bridge_checks_before_container_creation(monkeypatch, denied):
    from hcuopt.deployment import bw20_stage0_guards as guards
    from tests.unit.test_bw20_timing_session import Transport

    p, transport, checks = plan(), Transport(), []
    transport.read_proc = lambda path: "fixture"
    transport.read_namespace = lambda path: "fixture"

    def lease():
        checks.append("lease")
        if denied == "lease":
            raise RuntimeError("lease check denied")

    def source(bound_plan):
        assert bound_plan == p and not transport.calls
        checks.append("source")
        if denied == "source":
            raise RuntimeError("source check denied")

    monkeypatch.setattr(guards, "capture_process_binding", lambda **kw: dict(
        container_id=kw["container_id"], resource_id=p.resource_id,
        fencing_token=p.fencing_token, container_ready=kw["ready"]))
    context = dict(job_id=str(uuid4()), lease_id=str(uuid4()), lease_scope="exclusive",
                   resource_id=p.resource_id, fencing_token=p.fencing_token,
                   assert_live_lease=lease)
    session = guarded_job_session(plan=p, transport=transport, source_guard=source,
                                  context=context, cancelled=lambda: False)
    if denied:
        with pytest.raises(RuntimeError):
            session.open()
        assert not transport.calls
    else:
        session.open()
        assert checks[:3] == ["lease", "source", "lease"]
        session.close()
        assert session.cleanup_complete
