#!/usr/bin/env python3
"""Capture one real SGLang prefill trace for the nmz36 Stage 0 profiler gate.

This helper runs inside the digest-locked inference image.  It intentionally
uses SGLang's own ``bench_one_batch`` profiler path rather than manufacturing a
trace or profiling an unrelated tensor fixture.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

VERSION = "profile-llm-torch nmz36-sglang-prefill-v1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--input-len", type=int, default=128)
    parser.add_argument("--output-len", type=int, default=8)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = args.output
    if not output.is_absolute():
        raise SystemExit("--output must be absolute")
    if args.input_len < 1 or args.output_len < 2:
        raise SystemExit("input length must be positive and output length must be at least two")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink():
        raise SystemExit("trace output cannot be a symlink")
    output.unlink(missing_ok=True)

    prefix = output.parent / f".hcuopt-prefill-{uuid4().hex}"
    result_path = output.parent / f"bench-result-{uuid4().hex}.jsonl"
    command = (
        sys.executable,
        "-m",
        "sglang.bench_one_batch",
        "--model-path",
        args.model_path,
        "--tp-size",
        "1",
        "--trust-remote-code",
        "--attention-backend",
        "fa3",
        "--page-size",
        "64",
        "--mem-fraction-static",
        "0.85",
        "--batch-size",
        "1",
        "--input-len",
        str(args.input_len),
        "--output-len",
        str(args.output_len),
        "--result-filename",
        str(result_path),
        "--profile",
        "--profile-stage",
        "prefill",
        "--profile-record-shapes",
        "--profile-activities",
        "CPU",
        "GPU",
        "--profile-filename-prefix",
        str(prefix),
    )
    completed = subprocess.run(command, stdin=subprocess.DEVNULL, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"SGLang profiler workload exited with {completed.returncode}")

    traces = sorted(output.parent.glob(f"{prefix.name}*.trace.json.gz"))
    if len(traces) != 1 or traces[0].is_symlink() or not traces[0].is_file():
        raise SystemExit(f"expected exactly one fresh SGLang trace, found {len(traces)}")
    os.replace(traces[0], output)
    output.chmod(0o444)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
