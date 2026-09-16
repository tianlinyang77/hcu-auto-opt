# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from hcuopt.deployment import bw20_m1_staging as staging
from hcuopt.deployment.bw20_m1_runtime import build_m1_container_plan
from hcuopt.targets import load_target

ROOT = Path(__file__).resolve().parents[2]
RUN_ID = UUID("00000000-0000-0000-0000-000000000124")


def _target():
    return load_target(ROOT / "config/targets/bw20-sglang-0.5.12.yaml")


def _hash(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class _Guard:
    def __init__(self):
        self.plans = []

    def __call__(self, plan):
        self.plans.append(plan)


class _Transport:
    instances = []

    def __init__(self, runner):
        self.runner = runner
        self.commands = []
        self.copies = []
        type(self).instances.append(self)

    def checked(self, argv, timeout=60):
        del timeout
        self.commands.append(tuple(argv))
        if argv[:3] == ("python3", "-c", staging.INIT_OUTPUT):
            run = argv[3]
            return json.dumps(
                {
                    "schema_version": "bw20-m1-output-init-v1",
                    "run": run,
                    "evidence": run + "/evidence",
                    "cache": run + "/cache",
                }
            ).encode()
        if argv[0] == "getfacl":
            return b"user::rwx\nuser:65534:rwx\n"
        if argv[:3] == ("python3", "-c", staging.VERIFY_OUTPUT):
            run, arm, expected = argv[3:]
            return json.dumps(
                {
                    "schema_version": "bw20-m1-staging-v1",
                    "run": run,
                    "arm": arm,
                    "artifact_hash": None if expected == "none" else expected,
                    "evidence_empty": True,
                    "cache_empty": True,
                }
            ).encode()
        return b""

    def copy(self, source, destination, *, download=False):
        self.copies.append((source, destination, download))


@pytest.fixture(autouse=True)
def _reset_transport():
    _Transport.instances.clear()


def test_host_staging_programs_remain_python36_compatible() -> None:
    assert staging.host_scripts_are_python36_compatible()


def test_candidate_staging_reuses_frozen_controller_and_verifies_artifact(
    tmp_path, monkeypatch
) -> None:
    payload = b"# bounded candidate overlay\n"
    artifact = tmp_path / "candidate.py"
    artifact.write_bytes(payload)
    plan = build_m1_container_plan(
        _target(),
        run_id=RUN_ID,
        arm="candidate",
        acquisition_ordinal=1,
        fencing_token=9,
        artifact_hash=_hash(payload),
    )
    guard = _Guard()
    staged = []
    monkeypatch.setattr(
        staging,
        "stage_controller",
        lambda **kwargs: staged.append(kwargs) or guard,
    )
    monkeypatch.setattr(staging, "BW20Transport", _Transport)

    result = staging.stage_m1_run(
        plan=plan,
        bundle=SimpleNamespace(),
        runner=SimpleNamespace(),
        artifact=artifact,
    )

    assert result.receipt["artifact_hash"] == _hash(payload)
    assert staged[0]["plan"] == plan
    assert guard.plans == [plan]
    transport = _Transport.instances[0]
    assert transport.copies == [(artifact, plan.artifact_path, False)]
    assert sum(command[0] == "setfacl" for command in transport.commands) == 2


def test_local_artifact_copy_is_exclusive_and_hash_bound(tmp_path) -> None:
    payload = b"# bounded candidate overlay\n"
    source = tmp_path / "source.py"
    destination = tmp_path / "destination.py"
    source.write_bytes(payload)
    staging.copy_local_artifact(source, destination, _hash(payload))
    assert destination.read_bytes() == payload

    with pytest.raises((FileExistsError, RuntimeError)):
        staging.copy_local_artifact(source, destination, _hash(payload))
    with pytest.raises(RuntimeError, match="frozen pin"):
        staging.copy_local_artifact(source, tmp_path / "wrong.py", "sha256:" + "0" * 64)


def test_staging_rejects_artifact_hash_drift_before_remote_contact(
    tmp_path, monkeypatch
) -> None:
    artifact = tmp_path / "candidate.py"
    artifact.write_text("# changed\n")
    plan = build_m1_container_plan(
        _target(),
        run_id=RUN_ID,
        arm="candidate",
        acquisition_ordinal=1,
        fencing_token=9,
        artifact_hash="sha256:" + "0" * 64,
    )
    monkeypatch.setattr(
        staging,
        "stage_controller",
        lambda **kwargs: pytest.fail("remote staging must not start"),
    )

    with pytest.raises(ValueError, match="frozen Hash"):
        staging.stage_m1_run(
            plan=plan,
            bundle=SimpleNamespace(),
            runner=SimpleNamespace(),
            artifact=artifact,
        )


def test_baseline_staging_rejects_candidate_input(tmp_path) -> None:
    plan = build_m1_container_plan(
        _target(),
        run_id=RUN_ID,
        arm="baseline",
        acquisition_ordinal=0,
        fencing_token=9,
    )
    artifact = tmp_path / "unexpected.py"
    artifact.write_text("# no\n")

    with pytest.raises(ValueError, match="Baseline"):
        staging.stage_m1_run(
            plan=plan,
            bundle=SimpleNamespace(),
            runner=SimpleNamespace(),
            artifact=artifact,
        )
