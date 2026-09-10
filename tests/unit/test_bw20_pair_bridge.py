# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json
from contextlib import contextmanager
from unittest.mock import Mock
from uuid import uuid4

import pytest

from hcuopt.deployment.bw20_pair_bridge import (
    VARIANT_FILES,
    BW20PairCleaner,
    BW20Transport,
    DiagnosticRepositoryClient,
    RepositoryLeaseGuard,
)
from hcuopt.deployment.kernel_binary_inventory import file_hash
from hcuopt.domain.errors import ExecutionSafetyError, StaleFencingToken


class Repo:
    def __init__(self, row):
        self.row = row
        self.query = None

    @contextmanager
    def connection(self):
        yield self

    def execute(self, query, args):
        self.query = query
        return self

    def fetchone(self):
        return self.row


def test_guard_requires_active_unexpired_owned_job():
    repo = Repo({"ok": 1})
    guard = RepositoryLeaseGuard(repo, uuid4())
    guard.before_execute("fixture", 1)
    assert "clock_timestamp()" in repo.query and "j.lease_id=r.lease_id" in repo.query
    guard.before_result("fixture", 1)
    repo.row = None
    for check in (guard.before_execute, guard.before_result):
        with pytest.raises(StaleFencingToken):
            check("fixture", 1)


def test_expired_heartbeat_does_not_resurrect_lease():
    repository = Mock()
    guard = RepositoryLeaseGuard(Repo(None), uuid4())
    client = DiagnosticRepositoryClient(repository, guard)
    with pytest.raises(StaleFencingToken):
        client.heartbeat("worker", {"resource_id": "fixture", "fencing_token": 1})
    repository.heartbeat_job.assert_not_called()


@pytest.mark.parametrize("change", [{"busy": 1}, {"vram_bytes": 100000000},
                                  {"kfd_pids": ["123"]}, {"pci": "wrong"}, {"numa": 0}])
def test_foreign_activity_or_identity_blocks_launch(change):
    transport = BW20Transport(Mock())
    transport.state = lambda: {"busy": 0, "vram_bytes": 2207744,
                               "kfd_pids": [], "pci": "0000:b1:00.0", "numa": 4, **change}
    with pytest.raises(ExecutionSafetyError):
        transport.require_idle()


@pytest.mark.parametrize("tamper", ["none", "download", "during_copy", "normalized", "missing"])
def test_remote_evidence_readback(tmp_path, tamper):
    remote, local = tmp_path / "remote", tmp_path / "local"
    remote.mkdir()
    local.mkdir()
    normalized = {"text": " Paris", "finish_reason_type": "length",
                  "prompt_tokens": 5, "completion_tokens": 8}
    body = {"text": " Paris", "meta_info": {
        "finish_reason": {"type": "length"}, "prompt_tokens": 5, "completion_tokens": 8}}
    for name in VARIANT_FILES:
        (remote / name).write_text("{}")
    (remote / "result.json").write_text(json.dumps({
        "status": "succeeded", "normalized_output": {
            **normalized, **({"text": "wrong"} if tamper == "normalized" else {})}}))
    (remote / "response.json").write_text(json.dumps({"body_json": body}))
    hashes = {p.name: file_hash(p) for p in remote.iterdir()}
    if tamper == "missing":
        hashes.pop("server.log")
    transport = BW20Transport(Mock())
    changed = {**hashes, "server.log": "sha256:" + "f" * 64}
    transport.checked = Mock(side_effect=[json.dumps(hashes).encode(),
        json.dumps(changed if tamper == "during_copy" else hashes).encode()])

    def copy(source, destination, **kwargs):
        data = (remote / source.rsplit("/", 1)[-1]).read_bytes()
        destination.write_bytes(b"wrong" if tamper == "download" else data)

    transport.copy = copy
    root = f"/home/github/hcu-auto-opt-runtime/bw20-framework-smoke/{uuid4()}"
    if tamper == "none":
        assert transport.collect(root, "baseline", local) == hashes
    else:
        with pytest.raises(ExecutionSafetyError):
            transport.collect(root, "baseline", local)


def test_cleaner_selects_only_exact_pair_requests():
    from tests.unit.test_bw20_execution import TARGET
    transport = Mock()
    transport.checked.return_value = b""
    ids = (uuid4(), uuid4())
    cleaner = BW20PairCleaner(TARGET, transport, ids)
    from hcuopt.adapters.bw20_execution import RESOURCE_ID
    assert cleaner._managed_container_ids(RESOURCE_ID) == []
    for call, request_id in zip(transport.checked.call_args_list, ids, strict=True):
        assert f"label=io.hcuopt.request-id={request_id}" in call.args[0]
