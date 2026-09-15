# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Read-only recheck of prepared BW20 inputs, never a Stage0 admission grant.

The caller supplies the preparation hash from its independent retained receipt.
This is a point-in-time check, not protection against concurrent writers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.contracts.platform_v1 import ArtifactManifest, SourceSnapshot
from hcuopt.deployment.bw20_runtime_prepare import RELATIVE_MODULE
from hcuopt.deployment.bw20_stage0_guards import BW20SourceGuard
from hcuopt.deployment.bw20_stage0_harness import PROFILE
from hcuopt.evaluation.sglang_smoke import load_workload_spec
from hcuopt.runtime_probes.overlay import OverlayCapabilityProbe
from hcuopt.runtime_probes.profile import RuntimeProbeProfile
from hcuopt.source_hash import file_uri_to_path
from hcuopt.targets import load_target, target_fingerprint

INPUT_NAMES = frozenset({
    "target.json", "baseline.json", "candidate.json", "artifact.json", "workload.json",
})


def checked_digest(path: Path, *, limit: int = 16 * 1024**3) -> str:
    """Hash regular nonredirected files with bounded reads and replacement checks."""
    before = path.lstat()
    if (path.resolve(strict=True) != path or not stat.S_ISREG(before.st_mode)
            or before.st_size > limit):
        raise ValueError("unsafe or oversized input: " + str(path))
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("input replaced during open")
        total = 0
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            total += len(block)
            if total > limit:
                raise ValueError("input read budget exceeded")
            digest.update(block)
        after = os.fstat(stream.fileno())
    current = path.lstat()

    def identity(s):
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)

    # Windows Python 3.12 lstat/fstat disagree on ctime semantics. Compare ctime
    # only within the same API; keep exact inode/size/mtime checks across APIs.
    if (identity(before) != identity(after) or identity(after) != identity(current)
            or before.st_ctime_ns != current.st_ctime_ns
            or opened.st_ctime_ns != after.st_ctime_ns):
        raise ValueError("input changed during read")
    return "sha256:" + digest.hexdigest()


def _pinned(path: Path, expected: str) -> bytes:
    if not isinstance(expected, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", expected) is None:
        raise ValueError("independent SHA256 pin required")
    if checked_digest(path, limit=4 * 1024**2) != expected:
        raise ValueError("input differs from pin: " + path.name)
    with path.open("rb") as stream:
        raw = stream.read(4 * 1024**2 + 1)
    if "sha256:" + hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("input changed after hash: " + path.name)
    return raw


def verify_model_inventory(model: Path, inventory: dict) -> dict:
    if model.resolve(strict=True) != model or not model.is_dir() or not inventory:
        raise ValueError("invalid model directory or empty inventory")
    if len(inventory) > 10000:
        raise ValueError("model inventory budget exceeded")
    seen = set()
    total = 0
    for parent, dirs, files in os.walk(model, followlinks=False):
        for name in dirs:
            directory = Path(parent) / name
            if directory.is_symlink() or directory.resolve(strict=True) != directory:
                raise ValueError("redirected model directory")
        for name in files:
            path = Path(parent) / name
            relative = path.relative_to(model).as_posix()
            entry = inventory.get(relative)
            if not isinstance(entry, dict) or set(entry) != {"size", "sha256"}:
                raise ValueError("unexpected model member: " + relative)
            if type(entry["size"]) is not int or not 0 <= entry["size"] <= 16 * 1024**3:
                raise ValueError("invalid model member size")
            total += entry["size"]
            if total > 64 * 1024**3:
                raise ValueError("model total budget exceeded")
            if (checked_digest(path) != entry["sha256"]
                    or path.stat().st_size != entry["size"]):
                raise ValueError("model content differs: " + relative)
            seen.add(relative)
    if seen != set(inventory):
        raise ValueError("model inventory incomplete")
    return {"file_count": len(seen), "total_bytes": total}


def verify_preparation(*, root: Path, controller: Path, preparation_sha256: str, runner) -> dict:
    if root.resolve(strict=True) != root or controller.resolve(strict=True) != controller:
        raise ValueError("redirected preparation or controller directory")
    raw = _pinned(root / "preparation.json", preparation_sha256)
    preparation = json.loads(raw)
    if preparation["schema_version"] != "bw20-runtime-preparation-v1":
        raise ValueError("unsupported preparation receipt")
    if set(preparation["input_hashes"]) != INPUT_NAMES:
        raise ValueError("preparation input set differs")
    pins = dict(preparation["input_hashes"])
    pins["runtime-profile.json"] = preparation["profile_sha256"]
    pins["model-inventory.json"] = preparation["model_inventory_sha256"]
    content = {name: _pinned(root / name, pin) for name, pin in pins.items()}
    target = load_target(root / "target.json")
    baseline = SourceSnapshot.model_validate_json(content["baseline.json"])
    candidate = SourceSnapshot.model_validate_json(content["candidate.json"])
    artifact = ArtifactManifest.model_validate_json(content["artifact.json"])
    profile = RuntimeProbeProfile.model_validate_json(content["runtime-profile.json"])
    workload = load_workload_spec(root / "workload.json")
    fingerprint = target_fingerprint(target)
    if (fingerprint != preparation["target_fingerprint"]
            or profile.target_fingerprint != fingerprint
            or profile.target_id != target.target_id or workload.target_id != target.target_id
            or profile.profile != PROFILE or profile.hotpatch is None
            or profile.hotpatch.baseline_source != baseline
            or profile.hotpatch.candidate_source != candidate
            or profile.hotpatch.artifact != artifact):
        raise ValueError("prepared input binding differs")
    source_guard = BW20SourceGuard(
        runner=runner, source_root=str(controller),
        manifest_sha256=preparation["controller_manifest_sha256"],
    )
    source_guard(SimpleNamespace(source_root=str(controller)))
    manager = GitSourceManager(profile=PROFILE)
    for snapshot in (baseline, candidate):
        path = file_uri_to_path(snapshot.worktree_uri)
        if path.resolve(strict=True) != path:
            raise ValueError("redirected source worktree")
        manager._assert_snapshot_unchanged(snapshot, path)
    artifact_path = file_uri_to_path(artifact.uri)
    if checked_digest(artifact_path) != artifact.content_hash:
        raise ValueError("artifact bytes differ")
    implementation = file_uri_to_path(candidate.worktree_uri) / RELATIVE_MODULE
    if checked_digest(implementation) != artifact.content_hash:
        raise ValueError("artifact is not the candidate module")
    OverlayCapabilityProbe._validate_sources(baseline, candidate, artifact)
    model = verify_model_inventory(
        Path(workload.model_path), json.loads(content["model-inventory.json"]),
    )
    # Detect persistent metadata changes during the longer model/source pass.
    for name, pin in pins.items():
        _pinned(root / name, pin)
    _pinned(root / "preparation.json", preparation_sha256)
    source_guard(SimpleNamespace(source_root=str(controller)))
    return dict(
        schema_version="bw20-runtime-input-recheck-v1",
        observed_at=datetime.now(timezone.utc).isoformat(),
        input_integrity_passed=True,
        preparation_sha256=preparation_sha256,
        profile_sha256=preparation["profile_sha256"],
        target_fingerprint=fingerprint,
        controller_checks=source_guard.observations,
        model=model,
        baseline_source_hash=baseline.source_hash,
        candidate_source_hash=candidate.source_hash,
        artifact_sha256=artifact.content_hash,
        unresolved_blockers=[b.id for b in target.blockers if b.status == "open"],
        scope="point_in_time_input_integrity_only",
        runtime_module_verified=False, workload_executed=False,
        deployment_registered=False, hcu_used=False,
        stage0_accepted=False, automatic_release_allowed=False,
    )


@dataclass(frozen=True)
class BW20PreparedInputGuard:
    """Deployment-owned input pins; never derive these from a submitted Job."""

    root: Path
    controller: Path
    preparation_sha256: str
    runner: object

    def __call__(self):
        return verify_preparation(
            root=self.root, controller=self.controller,
            preparation_sha256=self.preparation_sha256, runner=self.runner,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--controller", type=Path, required=True)
    parser.add_argument("--preparation-sha256", required=True)
    args = parser.parse_args()
    from hcuopt.deployment.bw20_local_runner import BW20LocalCommandRunner

    try:
        report = verify_preparation(
            root=args.prepared, controller=args.controller,
            preparation_sha256=args.preparation_sha256, runner=BW20LocalCommandRunner(),
        )
    except Exception as exc:
        print(json.dumps(dict(input_integrity_passed=False, error_type=type(exc).__name__,
                              error=str(exc), stage0_accepted=False)))
        return 2
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
