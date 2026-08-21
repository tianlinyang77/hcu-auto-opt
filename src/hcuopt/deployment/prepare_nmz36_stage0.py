#!/usr/bin/env python3
"""Prepare the deployment-owned nmz36 Stage 0 runtime profile.

The command creates an isolated Candidate worktree, a semantics-preserving
Python overlay with a real import attestation marker, fixed profiler/overlay
commands, and host evidence directories.  It never changes the locked baseline
working tree or the inference image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from uuid import uuid4

import yaml

from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.profiles import REAL_STAGE0_PROFILE
from hcuopt.contracts.platform_v1 import ArtifactManifest
from hcuopt.runtime_probes.profile import RuntimeProbeProfile
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path
from hcuopt.targets import load_target, target_fingerprint

MODEL_PATH = "/public/opendas/DL_DATA/llm-models/qwen2.5/Qwen2.5-0.5B-Instruct"
REPLACEMENT_POINT = (
    "/usr/local/lib/python3.10/dist-packages/sglang/srt/layers/layernorm.py"
)
BASELINE_IMPLEMENTATION = "python/sglang/srt/layers/layernorm.py"
CANDIDATE_MARKER = "hcuopt-nmz36-layernorm-overlay-v1"

_IMPORT_MARKER = r'''

# HCUOPT Stage 0 import attestation. The candidate remains behavior-equivalent;
# this block only records that the real SGLang process group imported these bytes.
def _hcuopt_stage0_import_attestation():
    import hashlib as _hcuopt_hashlib
    import json as _hcuopt_json
    import os as _hcuopt_os

    _hcuopt_destination = _hcuopt_os.environ.get("HCUOPT_CANDIDATE_IMPORT_MARKER")
    if not _hcuopt_destination:
        return
    with open(__file__, "rb") as _hcuopt_source:
        _hcuopt_digest = "sha256:" + _hcuopt_hashlib.sha256(
            _hcuopt_source.read()
        ).hexdigest()
    _hcuopt_payload = {
        "protocol_version": "hcuopt-sglang-overlay-import-v1",
        "process_id": _hcuopt_os.getpid(),
        "process_group_id": _hcuopt_os.getpgrp(),
        "module_file": _hcuopt_os.path.realpath(__file__),
        "module_sha256": _hcuopt_digest,
    }
    _hcuopt_temporary = (
        f"{_hcuopt_destination}.{_hcuopt_os.getpid()}.tmp"
    )
    with open(_hcuopt_temporary, "w", encoding="utf-8") as _hcuopt_output:
        _hcuopt_json.dump(
            _hcuopt_payload,
            _hcuopt_output,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        _hcuopt_output.write("\n")
        _hcuopt_output.flush()
        _hcuopt_os.fsync(_hcuopt_output.fileno())
    _hcuopt_os.replace(_hcuopt_temporary, _hcuopt_destination)


_hcuopt_stage0_import_attestation()
del _hcuopt_stage0_import_attestation
'''


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-lock", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--workload-spec", required=True, type=Path)
    parser.add_argument("--deployment-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = load_target(args.target_lock)
    source_root = args.source_root.resolve(strict=True)
    deployment_root = args.deployment_root.resolve()
    deployment_root.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    if output.parent != deployment_root:
        raise SystemExit("runtime profile output must be directly below deployment root")

    baseline = GitSourceManager(REAL_STAGE0_PROFILE).prepare_baseline(
        target,
        deployment_root,
    )
    candidate_id = uuid4()
    candidate = GitSourceManager(REAL_STAGE0_PROFILE).create_candidate(
        baseline,
        candidate_id,
        deployment_root,
    )
    candidate_root = file_uri_to_path(candidate.worktree_uri)
    baseline_implementation = (
        file_uri_to_path(baseline.worktree_uri) / BASELINE_IMPLEMENTATION
    ).resolve(strict=True)
    artifact_path = candidate_root / "artifacts" / "hcuopt-layernorm-overlay.py"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(
        baseline_implementation.read_bytes() + _IMPORT_MARKER.encode("utf-8")
    )
    artifact_path.chmod(0o444)
    if _git(candidate_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise SystemExit("candidate worktree is not clean after writing the ignored Artifact")
    candidate = candidate.model_copy(
        update={"source_hash": canonical_source_hash(candidate_root), "clean": True}
    )
    if candidate.source_hash == baseline.source_hash:
        raise SystemExit("candidate source hash did not change")
    artifact_hash = _sha256_file(artifact_path)
    artifact = ArtifactManifest(
        candidate_id=candidate_id,
        kind="python_overlay",
        uri=artifact_path.resolve().as_uri(),
        content_hash=artifact_hash,
        source_snapshot_id=candidate.snapshot_id,
        synthetic=False,
    )

    workload_json = deployment_root / "nmz36-sglang-smoke-v1.json"
    workload = yaml.safe_load(args.workload_spec.read_text(encoding="utf-8"))
    if not isinstance(workload, dict):
        raise SystemExit("workload spec must contain one mapping")
    _write_json(workload_json, workload)

    profiler_dir = deployment_root / "profiler-evidence"
    profiler_dir.mkdir()
    trace_path = profiler_dir / "torch-trace.json.gz"
    phase_root = deployment_root / "overlay-evidence"
    phase_dirs = {
        name: phase_root / name for name in ("baseline", "candidate", "recovery")
    }
    for directory in phase_dirs.values():
        directory.mkdir(parents=True)

    source_mount = _mount(source_root, "/opt/hcuopt/source")
    model_mount = {
        "source": MODEL_PATH,
        "target": MODEL_PATH,
        "read_only": True,
    }
    hyhal_mount = {"source": "/opt/hyhal", "target": "/opt/hyhal", "read_only": True}
    workload_mount = _mount(workload_json, "/opt/hcuopt/workload.json")
    profiler_capture = "/opt/hcuopt/source/src/hcuopt/deployment/nmz36_profiler_capture.py"
    smoke_runner = "/opt/hcuopt/source/src/hcuopt/evaluation/sglang_smoke_runner.py"
    overlay_runner = "/opt/hcuopt/source/src/hcuopt/runtime_probes/sglang_overlay_runner.py"

    def phase(name: str) -> dict[str, object]:
        evidence_target = f"/evidence/{name}"
        cache = (
            "/tmp/hcuopt-stage0-candidate-cache"
            if name == "candidate"
            else "/tmp/hcuopt-stage0-baseline-cache"
        )
        argv = [
            "python",
            overlay_runner,
            "--phase",
            name,
            "--smoke-runner",
            smoke_runner,
            "--spec",
            "/opt/hcuopt/workload.json",
            "--evidence-dir",
            evidence_target,
            "--replacement-point",
            REPLACEMENT_POINT,
            "--activation-marker",
            CANDIDATE_MARKER,
            "--formal-evidence",
        ]
        environment = {
            "HCUOPT_CANDIDATE_CACHE_DIR": cache,
            "TRITON_CACHE_DIR": cache,
            "TORCHINDUCTOR_CACHE_DIR": cache,
            "PYTHONUNBUFFERED": "1",
        }
        if name == "candidate":
            marker_path = f"{evidence_target}/candidate-import.json"
            argv.extend(
                (
                    "--candidate-import-marker",
                    marker_path,
                    "--expected-artifact-hash",
                    artifact_hash,
                )
            )
            environment["HCUOPT_CANDIDATE_IMPORT_MARKER"] = marker_path
        implementation = artifact_path if name == "candidate" else baseline_implementation
        return {
            "argv": argv,
            "working_directory": "/workspace",
            "environment": environment,
            "timeout_seconds": 900,
            "mounts": [
                source_mount,
                workload_mount,
                model_mount,
                hyhal_mount,
                _mount(phase_dirs[name], evidence_target, read_only=False),
            ],
            "evidence_directory_uri": phase_dirs[name].resolve().as_uri(),
            "implementation_source_uri": implementation.resolve().as_uri(),
        }

    configuration = RuntimeProbeProfile(
        profile=REAL_STAGE0_PROFILE,
        target_id=target.target_id,
        target_fingerprint=target_fingerprint(target),
        profiler={
            "tool_candidates": [
                {
                    "name": "profile-llm-torch",
                    "version_argv": ["python", profiler_capture, "--version"],
                    "profile_argv": [
                        "python",
                        profiler_capture,
                        "--output",
                        "/evidence/torch-trace.json.gz",
                        "--model-path",
                        MODEL_PATH,
                    ],
                    "output_format": "torch_trace",
                    "output_path": "/evidence/torch-trace.json.gz",
                    "output_host_uri": trace_path.resolve().as_uri(),
                    "timeout_seconds": 900,
                }
            ],
            "profile_workload": "prefill",
            "warmup_steps": 1,
            "num_steps": 1,
            "prefill_input_len": 128,
            "prefill_output_len": 1,
            "working_directory": "/workspace",
            "environment": {"PYTHONUNBUFFERED": "1"},
            "mounts": [
                source_mount,
                model_mount,
                hyhal_mount,
                _mount(profiler_dir, "/evidence", read_only=False),
            ],
        },
        hotpatch={
            "workload_kind": "sglang_python_triton",
            "replacement_point": REPLACEMENT_POINT,
            "baseline_source": baseline,
            "candidate_source": candidate,
            "artifact": artifact,
            "overlay_mount_target": REPLACEMENT_POINT,
            "activation_marker": CANDIDATE_MARKER,
            "baseline": phase("baseline"),
            "candidate": phase("candidate"),
            "recovery": phase("recovery"),
        },
    )
    _write_json(output, configuration.model_dump(mode="json"))
    print(output)
    return 0


def _mount(source: Path, target: str, *, read_only: bool = True) -> dict[str, object]:
    return {
        "source": source.resolve(strict=True).as_posix(),
        "target": target,
        "read_only": read_only,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(path), *arguments),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.stderr.strip() or "Git command failed")
    return completed.stdout.strip()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
