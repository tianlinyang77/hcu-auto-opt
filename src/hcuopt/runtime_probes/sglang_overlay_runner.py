"""Run the fixed SGLang smoke workload and attest a Python/Triton startup overlay.

The candidate module must write the import marker from its real SGLang server process.
This wrapper verifies that marker against the mounted implementation and the server PID;
it never accepts an activation claim supplied by the Stage0Run caller.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

OVERLAY_PROTOCOL = "hcuopt-overlay-result-v2"
IMPORT_PROTOCOL = "hcuopt-sglang-overlay-import-v1"
SHA256_PREFIX = "sha256:"
MAX_JSON_BYTES = 1024 * 1024


def _regular_file(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("paths must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise argparse.ArgumentTypeError(f"file is unavailable: {path}") from error
    if path.is_symlink() or not resolved.is_file():
        raise argparse.ArgumentTypeError("path must be a regular non-symlink file")
    return resolved


def _sha256_bytes(value: bytes) -> str:
    return SHA256_PREFIX + hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return SHA256_PREFIX + digest.hexdigest()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
        raise RuntimeError(f"{label} must be a bounded regular JSON file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must contain one JSON object")
    return value


def _output_hash(normalized_output: Any) -> str:
    return _sha256_bytes(_canonical_json_bytes(normalized_output))


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_canonical_json(path: Path, value: Any) -> None:
    descriptor, name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o444)
    finally:
        temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("baseline", "candidate", "recovery"), required=True)
    parser.add_argument("--smoke-runner", required=True, type=_regular_file)
    parser.add_argument("--spec", required=True, type=_regular_file)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--replacement-point", required=True, type=_regular_file)
    parser.add_argument("--activation-marker", required=True)
    parser.add_argument("--candidate-import-marker", type=Path)
    parser.add_argument("--expected-artifact-hash")
    parser.add_argument("--formal-evidence", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    candidate_phase = args.phase == "candidate"
    if candidate_phase != bool(args.candidate_import_marker and args.expected_artifact_hash):
        raise SystemExit(
            "candidate phase requires import marker and expected artifact hash; "
            "baseline/recovery forbid them"
        )

    implementation_hash = _sha256_file(args.replacement_point)
    if candidate_phase and implementation_hash != args.expected_artifact_hash:
        raise SystemExit("mounted candidate hash does not match the frozen artifact")
    if args.candidate_import_marker is not None:
        args.candidate_import_marker.unlink(missing_ok=True)

    smoke_argv = [
        sys.executable,
        str(args.smoke_runner),
        "--spec",
        str(args.spec),
        "--evidence-dir",
        str(args.evidence_dir),
    ]
    if args.formal_evidence:
        restart_ordinal = {"baseline": 0, "candidate": 1, "recovery": 2}[args.phase]
        smoke_argv.extend(("--formal-lifecycle", "--restart-ordinal", str(restart_ordinal)))
    completed = subprocess.run(
        smoke_argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    result = _read_json(args.evidence_dir / "result.json", "SGLang result")
    start = _read_json(args.evidence_dir / "start.json", "SGLang start evidence")
    if completed.returncode != 0 or result.get("status") != "succeeded":
        raise SystemExit("fixed SGLang correctness workload failed")
    if result.get("cleanup_succeeded") is not True:
        raise SystemExit("SGLang runner did not prove process cleanup")
    server_pid = start.get("pid")
    if args.formal_evidence:
        lifecycle_start = _read_json(
            args.evidence_dir / "process-start.json",
            "process lifecycle start evidence",
        )
        _read_json(
            args.evidence_dir / "process-exit.json",
            "process lifecycle exit evidence",
        )
        server_pid = lifecycle_start.get("process_id")
    server_process_group = start.get("process_group_id")
    if isinstance(server_pid, bool) or not isinstance(server_pid, int) or server_pid < 1:
        raise SystemExit("SGLang start evidence has no valid server PID")
    if (
        isinstance(server_process_group, bool)
        or not isinstance(server_process_group, int)
        or server_process_group < 1
    ):
        raise SystemExit("SGLang start evidence has no valid process group")

    observation = {
        "protocol_version": OVERLAY_PROTOCOL,
        "activation_marker": "baseline",
        "output_hash": _output_hash(result.get("normalized_output")),
        "workload_kind": "sglang_python_triton",
        "replacement_point": str(args.replacement_point),
        "implementation_hash": implementation_hash,
        "process_id": server_pid,
    }
    if candidate_phase:
        marker = _read_json(args.candidate_import_marker, "candidate import marker")
        if marker.get("protocol_version") != IMPORT_PROTOCOL:
            raise SystemExit("candidate import marker protocol is invalid")
        marker_pid = marker.get("process_id")
        if isinstance(marker_pid, bool) or not isinstance(marker_pid, int) or marker_pid < 1:
            raise SystemExit("candidate import marker has no valid process ID")
        if marker.get("process_group_id") != server_process_group:
            raise SystemExit("candidate module was not imported by the SGLang server process group")
        if marker.get("module_file") != str(args.replacement_point):
            raise SystemExit("candidate import marker names a different replacement point")
        if marker.get("module_sha256") != implementation_hash:
            raise SystemExit("candidate import marker hash does not match the loaded module")
        observation.update(
            {
                "activation_marker": args.activation_marker,
                "loaded_artifact_hash": implementation_hash,
            }
        )

    if args.formal_evidence:
        _write_canonical_json(
            args.evidence_dir / "normalized-output.json",
            result.get("normalized_output"),
        )
        cache_directory = os.environ.get("HCUOPT_CANDIDATE_CACHE_DIR")
        if not cache_directory:
            raise SystemExit("Formal overlay requires HCUOPT_CANDIDATE_CACHE_DIR")
        _write_canonical_json(
            args.evidence_dir / "cache-namespace.json",
            {"cache_directory": cache_directory},
        )

    print(json.dumps(observation, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
