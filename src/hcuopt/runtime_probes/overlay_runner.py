"""Standalone correctness runner for the reversible startup-overlay probe.

This file is mounted read-only into the locked target image and invoked by path, so it
deliberately uses only the Python standard library and does not import ``hcuopt``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

PROTOCOL_VERSION = "hcuopt-overlay-result-v2"


def _regular_file(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("artifact paths must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise argparse.ArgumentTypeError(f"artifact is unavailable: {path}") from error
    if path.is_symlink() or not resolved.is_file():
        raise argparse.ArgumentTypeError("artifact must be a regular non-symlink file")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-artifact", required=True, type=_regular_file)
    parser.add_argument("--candidate-artifact", type=_regular_file)
    parser.add_argument("--activation-marker", required=True)
    parser.add_argument("--replacement-point", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    selected = args.candidate_artifact or args.baseline_artifact
    output_hash = _sha256(selected)
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "activation_marker": (
            args.activation_marker if args.candidate_artifact else "baseline"
        ),
        "output_hash": output_hash,
        "workload_kind": "generic_artifact_mount",
        "replacement_point": args.replacement_point,
        "implementation_hash": output_hash,
        "process_id": os.getpid(),
    }
    if args.candidate_artifact:
        payload["loaded_artifact_hash"] = output_hash
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
