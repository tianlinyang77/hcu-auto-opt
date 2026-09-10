# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Print a bounded environment-probe command; never execute or register an adapter."""

from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path

from hcuopt.contracts.platform_v1 import TargetSpec
from hcuopt.targets import load_target, target_fingerprint

IMAGE = (
    "10.16.1.152:5000/jenkins/model_test_env/sglang@"
    "sha256:a959b1d27fa7fada705bcb619331b0bc9a41bd67a7c2462b515a77718f108f1c"
)
PROBE = """import importlib.metadata as m, json, sys, torch
expected = json.loads(sys.argv[1])
assert list(sys.version_info[:2]) == [3, 10], "Python version mismatch"
assert m.version("sglang") == expected["sglang_version"], "SGLang version mismatch"
count = torch.cuda.device_count()
assert count == 1, "expected exactly one visible device; no allocation performed"
device = torch.cuda.get_device_properties(0)
assert (device.pci_domain_id, device.pci_bus_id, device.pci_device_id) == (0, 177, 0), \
    "physical PCI device mismatch; no allocation performed"
assert device.gcnArchName.split(":")[0] == "gfx936", "architecture mismatch"
x = torch.ones(1, device="cuda:0")
torch.cuda.synchronize()
assert x.item() == 1.0, "functional result mismatch"
print(json.dumps({"target_fingerprint": expected["target_fingerprint"],
    "python": sys.version, "sglang": m.version("sglang"), "torch": torch.__version__,
    "hip": torch.version.hip, "visible_devices": count, "device": str(device),
    "functional_probe": "passed", "performance_conclusion": "not_measured",
    "automatic_release_allowed": False}))
"""


def build_probe_plan(
    target: TargetSpec, *, container_name: str = "hcuopt-bw20-envcheck"
) -> dict[str, object]:
    """Compile only the reviewed BW20 single-device diagnostic scope."""
    host = target.execution_host
    card = host.accelerator
    if (
        target.target_id != "bw20-sglang-0.5.12"
        or (host.name, host.address) != ("github-bw20", "10.17.1.20")
        or (card.device_index, card.numa_node, card.cpu_affinity, card.architecture)
        != (7, 4, "64-79", "gfx936")
        or target.inference_image.immutable_reference != IMAGE
    ):
        raise ValueError("environment probe does not match the reviewed BW20 target")
    if [mount.model_dump() for mount in host.runtime_mounts] != [
        {"source": "/opt/hyhal", "target": "/opt/hyhal", "read_only": True}
    ]:
        raise ValueError("environment probe requires only the read-only HYHAL mount")
    if re.fullmatch(r"hcuopt-bw20-envcheck(?:-[a-z0-9-]+)?", container_name) is None:
        raise ValueError("invalid environment-probe container name")
    fingerprint = target_fingerprint(target)
    expected = json.dumps({
        "sglang_version": target.inference_image.sglang_package_version,
        "target_fingerprint": fingerprint,
    }, sort_keys=True)
    argv = [
        "docker", "run", "--rm", "--pull=never", "--name", container_name,
        "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--cpuset-cpus=64-79", "--cpuset-mems=4",
        "--memory=4g", "--pids-limit=128", "--device=/dev/kfd",
        "--device=/dev/dri/renderD135",
        "--mount", "type=bind,src=/opt/hyhal,dst=/opt/hyhal,readonly",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g", "--shm-size=1g", "-e", "HOME=/tmp",
        "--entrypoint", "/usr/bin/timeout", IMAGE,
        "--signal=TERM", "--kill-after=10s", "120s", "python3", "-E", "-c", PROBE, expected,
    ]
    return {
        "purpose": "environment_probe_only", "executed": False,
        "target_id": target.target_id, "target_fingerprint": fingerprint,
        "execution_host": host.name, "expected_image_id": target.inference_image.image_id,
        "argv": argv, "stage0_accepted": False, "production_profile_registered": False,
        "performance_conclusion": "not_measured", "automatic_release_allowed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target_lock", type=Path)
    parser.add_argument("--format", choices=("json", "shell"), default="json")
    parser.add_argument("--container-name", default="hcuopt-bw20-envcheck")
    args = parser.parse_args(argv)
    plan = build_probe_plan(load_target(args.target_lock), container_name=args.container_name)
    print(shlex.join(plan["argv"]) if args.format == "shell" else json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
