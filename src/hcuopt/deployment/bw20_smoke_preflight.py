# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Standalone, read-only image/model checks before the existing smoke runner.

This is a functional diagnostic, not Stage 0 or a Formal evaluator. Mount this
file and sglang_smoke_runner.py read-only; no project installation is needed.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Any

MODEL = "/public/opendas/DL_DATA/llm-models/qwen2.5/Qwen2.5-0.5B-Instruct"
MODEL_HASHES = {
    "config.json": "18e18afcaccafade98daf13a54092927904649e1dd4eba8299ab717d5d94ff45",
    "configuration.json": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    "generation_config.json": "e558847a8b4402616f1273797b015104dc266fe4b520056fca88823ba8f8ebe6",
    "model.safetensors": "fdf756fa7fcbe7404d5c60e26bff1a0c8b8aa1f72ced49e7dd0210fe288fb7fe",
    "tokenizer_config.json": "5b5d4f65d0acd3b2d56a35b56d374a36cbc1c8fa5cf3b3febbbfabf22f359583",
    "tokenizer.json": "c0382117ea329cdf097041132f6d735924b697924d6f6fc3945713e96ce87539",
    "vocab.json": "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910",
    "merges.txt": "599bab54075088774b1733fde865d5bd747cbcc7a547c5bc12610e874e26f5e3",
}
VERSION = "0.5.12+das.opt1.dtk2604.torch2100.2606021957.gdad582.hcuopt1"
WHEEL = f"/opt/hcuopt-image/sglang-{VERSION}-cp310-cp310-linux_x86_64.whl"
WHEEL_HASH = "20f3fef4bf87e9afd44d71ffabf4b26ec34934e68a8fad934d06d3c8634949aa"
ALLOCATOR = "sglang/srt/mem_cache/allocator.py"
ALLOCATOR_HASH = "ef09cd90dd03a542e586c70b4baa805b9d9ab24f84e4e8c20b6fc313f6f5fe27"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_scratch() -> dict[str, Any]:
    """Record actual mount flags; writable alone is insufficient for JIT libraries."""
    stat = os.statvfs("/tmp")
    return {"path": "/tmp", "statvfs_flags": stat.f_flag,
            "noexec": bool(stat.f_flag & os.ST_NOEXEC),
            "available_bytes": stat.f_bavail * stat.f_frsize}


def verify_model(directory: Path) -> dict[str, str]:
    # Extra code/config could change what trust_remote_code loads. Fail closed.
    files = {p.name for p in directory.iterdir() if p.is_file() or p.is_symlink()}
    require(files == set(MODEL_HASHES), "model file inventory changed")
    require(not any(p.is_dir() for p in directory.iterdir()), "unexpected model directory")
    observed = {}
    for name, expected in MODEL_HASHES.items():
        path = directory / name
        require(not path.is_symlink(), f"model symlink is not allowed: {name}")
        observed[name] = sha256(path)
        require(observed[name] == expected, f"model hash mismatch: {name}")
    return observed


def verify_runtime() -> dict[str, Any]:
    require(sys.version_info[:2] == (3, 10), "Python 3.10 is required")
    dist = importlib.metadata.distribution("sglang")
    require(dist.version == VERSION, "SGLang package version mismatch")
    require(sha256(Path(WHEEL)) == WHEEL_HASH, "retained wheel hash mismatch")
    installed = sha256(Path(dist.locate_file(ALLOCATOR)))
    require(installed == ALLOCATOR_HASH, "installed allocator differs from frozen source")
    require(sha256(Path("/source/python") / ALLOCATOR) == ALLOCATOR_HASH,
            "mounted source allocator changed")
    with zipfile.ZipFile(WHEEL) as wheel:
        require(hashlib.sha256(wheel.read(ALLOCATOR)).hexdigest() == installed,
                "wheel allocator differs from installed allocator")
    # Read device identity only here; the separate runner starts a fresh process.
    import torch

    require(torch.cuda.device_count() == 1, "expected exactly one visible HCU")
    device = torch.cuda.get_device_properties(0)
    require((device.pci_domain_id, device.pci_bus_id, device.pci_device_id) == (0, 177, 0),
            "expected physical HCU7 at PCI 0000:b1:00.0")
    require(device.gcnArchName.split(":")[0] == "gfx936", "architecture mismatch")
    return {"sglang_version": dist.version, "wheel_sha256": WHEEL_HASH,
            "allocator_sha256": installed, "visible_devices": 1, "pci": "0000:b1:00.0",
            "architecture": device.gcnArchName,
            "full_source_runtime_equivalence": "not_verified"}


def main() -> int:
    output = Path("/work/output")
    require(output.is_dir() and not any(output.iterdir()), "output must be a fresh empty directory")
    record: dict[str, Any] = {
        "purpose": "single_baseline_model_smoke_only", "status": "failed",
        "stage0_accepted": False, "performance_conclusion": "not_measured",
        "framework_pair_accepted": False, "automatic_release_allowed": False,
    }
    try:
        record["scratch"] = inspect_scratch()
        require(not record["scratch"]["noexec"],
                "JIT cache mount /tmp is noexec; model startup was not attempted")
        spec_path = Path("/work/input/spec.json")
        raw = json.loads(spec_path.read_text(encoding="utf-8"))
        require(raw["model_path"] == MODEL, "model path mismatch")
        require(raw["target_id"] == "bw20-sglang-0.5.12", "target mismatch")
        require(raw["workload_id"] == "bw20-sglang-baseline-preflight-v1", "workload mismatch")
        record["spec_sha256"] = sha256(spec_path)
        record["runner_sha256"] = sha256(Path("/work/input/sglang_smoke_runner.py"))
        record["preflight_sha256"] = sha256(Path(__file__))
        record["model_hashes"] = verify_model(Path(MODEL))
        record["runtime"] = verify_runtime()
        record["status"] = "passed"
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    (output / "preflight.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    if record["status"] != "passed":
        print(json.dumps(record), flush=True)
        return 2
    try:
        runner_path = Path("/work/input/sglang_smoke_runner.py")
        module_spec = importlib.util.spec_from_file_location("locked_smoke_runner", runner_path)
        require(module_spec is not None and module_spec.loader is not None, "missing smoke runner")
        runner = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(runner)
        result = runner.run_smoke(raw, output / "baseline", enable_child_subreaper=True)
    except Exception as exc:
        result = {"status": "failed", "cleanup_succeeded": False,
                  "error": f"{type(exc).__name__}: {exc}"}
    normalized = result.get("normalized_output") or {}
    passed = (result["status"] == "succeeded" and result["cleanup_succeeded"]
              and bool(normalized.get("text", "").strip())
              and normalized.get("completion_tokens", 0) > 0
              and normalized.get("finish_reason_type") in {"length", "stop"})
    summary = {**record, "status": "passed" if passed else "failed", "smoke": result}
    (output / "model-smoke.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
