# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""CPU driver through actual Git/Overlay/PostgreSQL, with signed fixture authority."""

import json
import os
from uuid import uuid4

import pytest

from hcuopt.api.formal_start_management import FormalStartManagement
from hcuopt.contracts.v1 import WorkerRegister
from hcuopt.deployment.formal_build_driver import FormalBuildDriver
from hcuopt.deployment.formal_runtime import FormalDeploymentRuntime
from hcuopt.domain.errors import Conflict
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path
from tests.integration.test_formal_dispatch_postgres import dispatch_case  # noqa: F401
from tests.integration.test_formal_real_builder_postgres import real_sources  # noqa: F401
from tests.integration.test_formal_start_management_postgres import isolated_dsn  # noqa: F401

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.getenv("HCUOPT_DATABASE_URL"), reason="requires PostgreSQL"),
    pytest.mark.skipif(os.name == "nt", reason="real readonly publication requires Linux"),
]


@pytest.mark.parametrize("mode", [
    "success", "budget", "stop", "wrong_owner", "unregistered", "partial_stop",
])
def test_driver_complete_family_and_fail_closed(request, tmp_path, mode, monkeypatch):
    source = request.getfixturevalue("real_sources")
    dispatcher, intent_id = request.getfixturevalue("dispatch_case")
    repo = dispatcher.repository
    runtime = FormalDeploymentRuntime(repo, FormalStartManagement(dispatcher.coordinator, ()),
                                      enabled=True)
    driver = FormalBuildDriver(
        runtime, artifact_root=tmp_path / "driver-artifacts",
        cache_root=tmp_path / "driver-cache", output_root=tmp_path / "driver",
    )
    if mode != "unregistered":
        repo.register_worker(WorkerRegister(worker_id="driver-worker", worker_type="build",
                                           adapter_profile=source["profile"]))
    claim = driver.start(intent_id, "driver-worker")
    args = dict(intent_id=intent_id, worker_id="driver-worker", claim_token=claim["claim_token"],
                wall_seconds_per_candidate=60)
    if mode == "partial_stop":
        build_one = driver.build_candidate
        def stop_after_first(**kwargs):
            result = build_one(**kwargs)
            runtime.claims.request_stop(intent_id, requested_by="test-between-members")
            return result
        monkeypatch.setattr(driver, "build_candidate", stop_after_first)
        with pytest.raises(Conflict, match="stop"):
            driver.build_family(**args)
        with repo.connection() as conn:
            assert conn.execute("SELECT count(*) AS n FROM artifacts").fetchone()["n"] == 1
            assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 1
            completed = conn.execute(
                "SELECT count(*) AS n FROM jobs WHERE state='succeeded'",
            ).fetchone()
            assert completed["n"] == 1
        return
    if mode == "budget":
        args["wall_seconds_per_candidate"] = 1e9
    elif mode == "stop":
        runtime.claims.request_stop(intent_id, requested_by="test-operator")
    elif mode == "wrong_owner":
        args["claim_token"] = uuid4()
    if mode != "success":
        with pytest.raises(Conflict):
            driver.build_family(**args)
        with repo.connection() as conn:
            assert conn.execute("SELECT count(*) AS n FROM formal_build_journal").fetchone()[
                "n"
            ] == 0
            assert conn.execute("SELECT count(*) AS n FROM artifacts").fetchone()["n"] == 0
        return
    first = driver.build_family(**args)
    assert len(first) == 2
    assert driver.build_family(**args) == first
    # The command is tested against the same real runtime. Configuration admission
    # is covered separately; do not label this a config-to-HCU acceptance test.
    from hcuopt.deployment import formal_build_command as command

    invocation = tmp_path / "invocation.json"
    invocation.write_text(json.dumps({
        "schema_version": "formal-build-invocation-v1",
        **{k: str(v) for k, v in args.items() if k != "wall_seconds_per_candidate"},
        "wall_seconds_per_candidate": 60,
        "artifact_root": "driver-artifacts", "cache_root": "driver-cache", "output_root": "driver",
    }), encoding="utf-8")
    invocation.chmod(0o600)
    monkeypatch.setattr(command, "load_formal_service_runtime", lambda **_: (runtime, None, None))
    report = command.execute_build_command(
        deployment_root=tmp_path, configuration_path=tmp_path / "not-used.json",
        invocation_path=invocation, database_url="not-used", expected_source_commit="a" * 40,
    )
    assert len(report["candidates"]) == 2
    assert report["hcu_accessed"] is False
    assert str(claim["claim_token"]) not in json.dumps(report)
    assert {file_uri_to_path(result.build.artifact.uri).read_bytes() for result in first} == (
        set(source["contents"])
    )
    baseline_path = file_uri_to_path(source["baseline"].worktree_uri)
    assert canonical_source_hash(baseline_path) == source["baseline"].source_hash
    assert not any((tmp_path / "driver" / str(intent_id) / "worktrees").iterdir())
    with pytest.raises(Conflict, match="already claimed"):
        driver.start(intent_id, "driver-worker")
    with pytest.raises(Conflict, match="other inputs"):
        driver.build_family(**{**args, "wall_seconds_per_candidate": 61})
    with repo.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM jobs WHERE state='succeeded'").fetchone()[
            "n"
        ] == 2
        assert conn.execute("SELECT count(*) AS n FROM formal_build_journal").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM round_budget_ledger "
                            "WHERE entry_type='reserve'").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM round_budget_ledger "
                            "WHERE entry_type='settle'").fetchone()["n"] == 2
