# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Prepare a reviewable model-smoke bundle locally; never run SSH or Docker."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
from pathlib import Path

from hcuopt.deployment.bw20_environment import IMAGE, build_probe_plan
from hcuopt.deployment.bw20_smoke_preflight import MODEL, MODEL_HASHES, sha256
from hcuopt.evaluation.sglang_smoke import load_workload_spec
from hcuopt.evaluation.sglang_smoke_runner import validate_spec
from hcuopt.targets import load_target

REMOTE = "/home/github/hcu-auto-opt-runtime/bw20-model-smoke-20260909"
NAME = "hcuopt-bw20-model-smoke-20260909"


def prepare_bundle(repo: Path, destination: Path) -> dict[str, object]:
    target = load_target(repo / "config/targets/bw20-sglang-0.5.12.yaml")
    base = build_probe_plan(target)  # Keep the original target/mount/topology guards.
    workload = load_workload_spec(repo / "config/workloads/nmz36-sglang-smoke-v1.yaml")
    spec = workload.model_dump(mode="json")
    spec.update(workload_id="bw20-sglang-baseline-preflight-v1", target_id=target.target_id)
    validate_spec(spec)
    if spec["model_path"] != MODEL or spec["tensor_parallel_size"] != 1:
        raise ValueError("baseline smoke workload drifted from the reviewed single-card scope")
    destination.mkdir(parents=True, exist_ok=False)
    inputs = destination / "input"
    inputs.mkdir()
    (inputs / "spec.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    for relative in (
        "src/hcuopt/deployment/bw20_smoke_preflight.py",
        "src/hcuopt/evaluation/sglang_smoke_runner.py",
    ):
        shutil.copyfile(repo / relative, inputs / Path(relative).name)
    argv = [
        "docker", "run", "--rm", "--pull=never", "--name", NAME,
        "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--cpuset-cpus=64-79", "--cpuset-mems=4",
        "--memory=16g", "--memory-swap=16g", "--pids-limit=512",
        "--device=/dev/kfd", "--device=/dev/dri/renderD135",
        "--mount", "type=bind,src=/opt/hyhal,dst=/opt/hyhal,readonly",
        "--mount", f"type=bind,src={REMOTE}/input,dst=/work/input,readonly",
        "--mount", f"type=bind,src={REMOTE}/output,dst=/work/output",
        "--mount", "type=bind,src=/home/github/hcu-auto-opt-runtime/sglang-das/"
        "python/sglang/srt/mem_cache/allocator.py,"
        "dst=/source/python/sglang/srt/mem_cache/allocator.py,readonly",
    ]
    for name in MODEL_HASHES:
        argv.extend(["--mount", f"type=bind,src={MODEL}/{name},dst={MODEL}/{name},readonly"])
    argv += [
        "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=4g", "--shm-size=2g",
        "--ulimit", "fsize=2147483648:2147483648", "--workdir=/work",
    ]
    for value in (
        "HOME=/tmp", "XDG_CACHE_HOME=/tmp/cache", "HF_HOME=/tmp/huggingface",
        "HF_HUB_OFFLINE=1", "TRANSFORMERS_OFFLINE=1", "TRITON_CACHE_DIR=/tmp/triton",
        "OMP_NUM_THREADS=8", "PYTHONNOUSERSITE=1", "PYTHONPATH=", "PYTHONHOME=",
        "PYTHONOPTIMIZE=0", "HIP_VISIBLE_DEVICES=0", "ROCR_VISIBLE_DEVICES=0",
        "HSA_VISIBLE_DEVICES=0",
    ):
        argv.extend(["-e", value])
    argv += ["--entrypoint", "/usr/bin/timeout", IMAGE, "--signal=TERM",
             "--kill-after=10s", "480s", "python3", "-I",
             "/work/input/bw20_smoke_preflight.py"]
    plan = {**{k: v for k, v in base.items() if k != "argv"},
            "purpose": "single_baseline_model_smoke_only", "argv": argv,
            "requires_new_container_scope_approval": True,
            "framework_pair_accepted": False, "remote_directory": REMOTE,
            "input_sha256": {p.name: sha256(p) for p in sorted(inputs.iterdir())},
            "memory_fraction_static": spec["mem_fraction_static"]}
    (destination / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    # Name collision is an error. Never delete an existing container or overwrite evidence.
    script = "\n".join([
        "#!/usr/bin/env bash", "set -euo pipefail",
        "# Execute on the reviewed host ONLY after approval and a fresh HCU7 occupancy check.",
        'test "$(hostname)" = github-bw20',
        'test "$(basename "$(readlink -f /sys/class/drm/renderD135/device)")" = 0000:b1:00.0',
        'test "$(cat /sys/class/drm/renderD135/device/numa_node)" = 4',
        "test \"$(docker image inspect --format '{{.Id}}' " + shlex.quote(IMAGE) + ')" = '
        + shlex.quote(target.inference_image.image_id),
        f'if docker container inspect {NAME} >/dev/null 2>&1; then exit 2; fi',
        f"test -d {REMOTE}/input", f"mkdir {REMOTE}/output",
        # Root without DAC_OVERRIDE cannot write a host-user-owned bind mount.
        # Grant only the image's root UID access to this fresh evidence directory.
        f"setfacl -m u:0:rwx {REMOTE}/output",
        shlex.join(argv).replace(" --", " \\\n  --").replace(" -e ", " \\\n  -e "), "",
    ])
    (destination / "run-reviewed.sh").write_text(script, encoding="utf-8", newline="\n")
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare_bundle(args.repo, args.output), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
