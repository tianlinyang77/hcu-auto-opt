# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import hashlib
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from hcuopt.deployment import bw20_smoke_preflight as preflight
from hcuopt.deployment.bw20_model_smoke import REMOTE, prepare_bundle
from hcuopt.evaluation.sglang_smoke_runner import validate_spec

ROOT = Path(__file__).resolve().parents[2]


def test_bundle_reuses_existing_runner_and_retains_no_acceptance(tmp_path):
    destination = tmp_path / "bundle"
    plan = prepare_bundle(ROOT, destination)
    spec = json.loads((destination / "input/spec.json").read_text())
    validate_spec(spec)
    assert spec["target_id"] == "bw20-sglang-0.5.12"
    assert spec["workload_id"] == "bw20-sglang-baseline-preflight-v1"
    assert spec["max_new_tokens"] == 8
    assert spec["attention_backend"] == "fa3"
    assert (destination / "input/sglang_smoke_runner.py").read_bytes() == (
        ROOT / "src/hcuopt/evaluation/sglang_smoke_runner.py"
    ).read_bytes()
    for field in ("executed", "stage0_accepted", "framework_pair_accepted",
                  "automatic_release_allowed", "production_profile_registered"):
        assert plan[field] is False
    assert plan["requires_new_container_scope_approval"] is True
    assert plan["performance_conclusion"] == "not_measured"
    argv = plan["argv"]
    assert [a for a in argv if a.startswith("--device=")] == [
        "--device=/dev/kfd", "--device=/dev/dri/renderD135",
    ]
    mounts = [argv[i + 1] for i, item in enumerate(argv) if item == "--mount"]
    assert len(mounts) == 12
    assert [m for m in mounts if not m.endswith(",readonly")] == [
        f"type=bind,src={REMOTE}/output,dst=/work/output",
    ]
    assert all(f"src={preflight.MODEL}/{name},dst=" in " ".join(mounts)
               for name in preflight.MODEL_HASHES)
    assert "--memory=16g" in argv and "--memory-swap=16g" in argv
    assert "fsize=2147483648:2147483648" in argv
    assert "/tmp:rw,exec,nosuid,nodev,size=4g" in argv
    assert "--network=none" in argv and "--read-only" in argv and "--pull=never" in argv
    assert "--cpuset-cpus=64-79" in argv and "--cpuset-mems=4" in argv
    assert "480s" in argv and "--kill-after=10s" in argv
    assert "--privileged" not in argv and "--network=host" not in argv
    assert "HIP_VISIBLE_DEVICES=0" in argv and "-I" in argv
    script = (destination / "run-reviewed.sh").read_text()
    assert "docker rm" not in script and "docker stop" not in script
    assert "mkdir " + REMOTE + "/output" in script
    assert f"setfacl -m u:0:rwx {REMOTE}/output" in script
    assert "chmod 777" not in script
    before = (destination / "input/spec.json").read_bytes()
    with pytest.raises(FileExistsError):
        prepare_bundle(ROOT, destination)
    assert (destination / "input/spec.json").read_bytes() == before


def test_frozen_hashes_are_sha256():
    for digest in [*preflight.MODEL_HASHES.values(), preflight.WHEEL_HASH,
                   preflight.ALLOCATOR_HASH]:
        assert len(digest) == 64
        assert int(digest, 16) >= 0


@pytest.mark.parametrize("mutation", [None, "changed", "missing", "extra", "directory"])
def test_model_integrity_stops_drift(tmp_path, monkeypatch, mutation):
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_bytes(b"frozen")
    monkeypatch.setattr(preflight, "MODEL_HASHES", {
        "config.json": hashlib.sha256(b"frozen").hexdigest(),
    })
    if mutation == "changed":
        (model / "config.json").write_bytes(b"tampered")
    elif mutation == "missing":
        (model / "config.json").unlink()
    elif mutation == "extra":
        (model / "remote_code.py").write_bytes(b"code")
    elif mutation == "directory":
        (model / "extra").mkdir()
    if mutation is None:
        assert preflight.verify_model(model) == preflight.MODEL_HASHES
    else:
        with pytest.raises(ValueError, match="model"):
            preflight.verify_model(model)


@pytest.mark.parametrize("mutation", [None, "version", "wheel", "installed", "source",
                                      "count", "pci", "architecture"])
def test_runtime_integrity_and_physical_device_guard(tmp_path, monkeypatch, mutation):
    wheel = tmp_path / "sglang.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(preflight.ALLOCATOR, b"allocator")
    allocator = tmp_path / "allocator.py"
    allocator.write_bytes(b"allocator")
    frozen = hashlib.sha256(b"allocator").hexdigest()
    monkeypatch.setattr(preflight, "WHEEL", str(wheel))
    monkeypatch.setattr(preflight, "WHEEL_HASH", preflight.sha256(wheel))
    monkeypatch.setattr(preflight, "ALLOCATOR_HASH", frozen)
    if mutation == "wheel":
        wheel.write_bytes(b"bad")
    if mutation == "installed":
        allocator.write_bytes(b"bad")
    original_hash = preflight.sha256
    monkeypatch.setattr(preflight, "sha256", lambda p: (
        "wrong" if mutation == "source" else frozen
    ) if str(p).replace("\\", "/").startswith("/source/") else original_hash(p))
    dist = SimpleNamespace(version="wrong" if mutation == "version" else preflight.VERSION,
                           locate_file=lambda _: allocator)
    monkeypatch.setattr(preflight.importlib.metadata, "distribution", lambda _: dist)
    monkeypatch.setattr(sys, "version_info", (3, 10, 12))
    device = SimpleNamespace(pci_domain_id=0, pci_bus_id=0 if mutation == "pci" else 177,
                             pci_device_id=0,
                             gcnArchName="gfx938" if mutation == "architecture" else "gfx936")
    torch = SimpleNamespace(cuda=SimpleNamespace(
        device_count=lambda: 8 if mutation == "count" else 1,
        get_device_properties=lambda _: device,
    ))
    monkeypatch.setitem(sys.modules, "torch", torch)
    if mutation is None:
        result = preflight.verify_runtime()
        assert result["pci"] == "0000:b1:00.0"
        assert result["full_source_runtime_equivalence"] == "not_verified"
    else:
        with pytest.raises(ValueError):
            preflight.verify_runtime()


@pytest.mark.parametrize("case,exit_code", [
    ("valid", 0), ("preflight_failure", 2), ("empty", 1), ("zero_tokens", 1),
    ("cleanup_failed", 1), ("abort", 1), ("runner_exception", 1), ("noexec", 2),
])
def test_entrypoint_records_failure_and_requires_real_output(
    tmp_path, monkeypatch, case, exit_code,
):
    output = tmp_path / "output"
    output.mkdir()
    inputs = tmp_path / "input"
    inputs.mkdir()
    (inputs / "spec.json").write_text(json.dumps({
        "model_path": preflight.MODEL, "target_id": "bw20-sglang-0.5.12",
        "workload_id": "bw20-sglang-baseline-preflight-v1",
    }))
    result = {"status": "succeeded", "cleanup_succeeded": case != "cleanup_failed",
              "normalized_output": {
                  "text": "  " if case == "empty" else " Paris",
                  "completion_tokens": 0 if case == "zero_tokens" else 1,
                  "finish_reason_type": "abort" if case == "abort" else "length",
              }}
    # This is explicitly a local stub, never a real-device acceptance.
    body = "raise RuntimeError('runner failed')" if case == "runner_exception" else (
        f"return {result!r}"
    )
    (inputs / "sglang_smoke_runner.py").write_text(
        "def run_smoke(*args, **kwargs):\n    " + body + "\n"
    )
    monkeypatch.setattr(preflight, "Path", lambda p: (
        output if str(p) == "/work/output" else
        inputs / Path(p).name if str(p).startswith("/work/input/") else Path(p)
    ))
    monkeypatch.setattr(preflight, "verify_model", lambda _: {})
    monkeypatch.setattr(preflight, "inspect_scratch", lambda: {"noexec": case == "noexec"})

    def runtime():
        if case == "preflight_failure":
            raise ValueError("integrity failed")
        return {"full_source_runtime_equivalence": "not_verified"}

    monkeypatch.setattr(preflight, "verify_runtime", runtime)
    assert preflight.main() == exit_code
    if case in {"preflight_failure", "noexec"}:
        assert not (output / "model-smoke.json").exists()
        assert json.loads((output / "preflight.json").read_text())["status"] == "failed"
    else:
        summary = json.loads((output / "model-smoke.json").read_text())
        assert summary["status"] == ("passed" if case == "valid" else "failed")
        assert summary["framework_pair_accepted"] is False
        assert summary["stage0_accepted"] is False
        assert summary["automatic_release_allowed"] is False


@pytest.mark.parametrize("flags,noexec", [(0, False), (8, True), (14, True)])
def test_scratch_mount_flags_are_observed_not_assumed(monkeypatch, flags, noexec):
    monkeypatch.setattr(preflight.os, "ST_NOEXEC", 8, raising=False)
    monkeypatch.setattr(preflight.os, "statvfs", lambda _: SimpleNamespace(
        f_flag=flags, f_bavail=100, f_frsize=4096,
    ), raising=False)
    assert preflight.inspect_scratch() == {
        "path": "/tmp", "statvfs_flags": flags, "noexec": noexec,
        "available_bytes": 409600,
    }
