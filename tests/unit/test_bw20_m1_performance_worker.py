# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from hcuopt.contracts.platform_v1 import SourceSnapshot
from hcuopt.deployment import bw20_m1_performance_worker as deployment
from hcuopt.deployment.bw20_stage0_staging import ControllerBundle
from hcuopt.source_hash import canonical_source_hash
from hcuopt.targets import load_target

ROOT = Path(__file__).resolve().parents[2]
HASH = "sha256:" + "a" * 64


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _baseline(tmp_path: Path) -> tuple[Path, bytes]:
    root = tmp_path / "baseline"
    source = root / deployment.ALLOCATOR_RELATIVE_PATH
    source.parent.mkdir(parents=True)
    payload = b"def free(self):\n    return None\n"
    source.write_bytes(payload)
    _git(root, "init")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=HCU Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "test(source): add baseline",
    )
    snapshot = SourceSnapshot(
        snapshot_id=uuid4(),
        kind="baseline",
        repository="https://example.invalid/sglang.git",
        commit=_git(root, "rev-parse", "HEAD"),
        tree_hash=_git(root, "rev-parse", "HEAD^{tree}"),
        source_hash=canonical_source_hash(root),
        worktree_uri=root.as_uri(),
        clean=True,
    )
    snapshot_file = tmp_path / "baseline.json"
    snapshot_file.write_text(snapshot.model_dump_json(), encoding="utf-8")
    return snapshot_file, payload


def test_baseline_module_hash_rereads_the_frozen_source(tmp_path: Path) -> None:
    snapshot_file, payload = _baseline(tmp_path)
    assert deployment.baseline_module_hash(snapshot_file) == (
        "sha256:" + hashlib.sha256(payload).hexdigest()
    )


def test_build_worker_binds_unique_harness_and_auto_clock_observation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    trusted = tmp_path / "trusted"
    source = trusted / "controller"
    source.mkdir(parents=True)
    snapshot_file, _ = _baseline(tmp_path)
    target = load_target(ROOT / "config/targets/bw20-sglang-0.5.12.yaml")
    calls = {}

    class Runner:
        pass

    class Guard:
        def __init__(self, runner):
            calls["guard_runner"] = runner
            self.observations = []

        def __call__(self, resource_id):
            calls["guard_resource"] = resource_id
            self.observations.append(
                {
                    "snapshot": {
                        "device": {
                            "performance_level": "auto",
                            "sclk_mhz": 600.0,
                            "mclk_mhz": 1800.0,
                        }
                    }
                }
            )

    def freeze(_source, archive):
        archive.write_bytes(b"controller")
        return ControllerBundle(archive, HASH, HASH, 1)

    registry = SimpleNamespace(profile="bw20-m1-manual-v1")

    def compose(**kwargs):
        calls["composition"] = kwargs
        return SimpleNamespace(registry=registry)

    class Worker:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    monkeypatch.setattr(deployment, "BW20LocalCommandRunner", Runner)
    monkeypatch.setattr(deployment, "BW20M1IdleGuard", Guard)
    monkeypatch.setattr(deployment, "freeze_controller", freeze)
    monkeypatch.setattr(deployment, "compose_bw20_m1_measurement", compose)
    monkeypatch.setattr(deployment, "Worker", Worker)

    worker = deployment.build_worker(
        worker_id="performance-worker",
        api_url="http://127.0.0.1:8000",
        target=target,
        source_root=source,
        baseline_snapshot_file=snapshot_file,
        trusted_evidence_root=trusted,
        output_dir=trusted / "output",
    )

    assert calls["guard_resource"] == deployment.BW20_M1_POLICY.resource_id
    assert calls["composition"]["initial_clock_state"] == {
        "mode": "auto",
        "sclk_mhz": 600.0,
        "mclk_mhz": 1800.0,
    }
    assert worker.kwargs["adapters"] is registry
    assert worker.kwargs["resource_guard"].observations
    assert worker.kwargs["capabilities"]["resource_id"] == (
        deployment.BW20_M1_POLICY.resource_id
    )
    assert worker.kwargs["capabilities"]["clock_mutation_performed"] is False
    assert worker.kwargs["capabilities"]["automatic_release_allowed"] is False
