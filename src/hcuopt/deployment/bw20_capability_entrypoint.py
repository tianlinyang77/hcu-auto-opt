# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Check actual module bytes and physical device before the original C workload."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

VERSION = "profile-llm-torch bw20-sglang-prefill-v1"


def verify_module(path: Path, expected: str):
    if path.resolve(strict=True) != path or not path.is_file():
        raise ValueError("runtime module must be a nonredirected regular file")
    if "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError("runtime module differs from frozen implementation")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--kind", choices=("profiler", "overlay"), required=True)
    parser.add_argument("--module", type=Path, required=True)
    parser.add_argument("--expected-hash", required=True)
    parser.add_argument("workload_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    verify_module(args.module, args.expected_hash)
    # The controller can import this module without initializing HIP.
    import torch

    from hcuopt.deployment.bw20_stage0_worker import validate_device

    validate_device(torch)
    workload_args = args.workload_args
    if workload_args[:1] == ["--"]:
        workload_args = workload_args[1:]
    if args.kind == "profiler":
        # Reuse the target-independent SGLang bench_one_batch argv/trace handling,
        # not the nmz36 target, profile or acceptance evidence.
        from hcuopt.deployment.nmz36_profiler_capture import main as capture
    else:
        from hcuopt.runtime_probes.sglang_overlay_runner import main as capture
    return capture(workload_args)


if __name__ == "__main__":
    sys.exit(main())
