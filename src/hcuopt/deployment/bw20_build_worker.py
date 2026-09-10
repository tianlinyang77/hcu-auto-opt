# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""CPU-container entrypoint for the ORIGINAL source/build JobHandlers.

No HCU, API, signing or release authority. The deployment transports one trusted
job envelope and reports the returned original contracts through the usual API.
This module never interprets candidate code or extracts an archive.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import UUID

from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.local_artifact_store import LocalArtifactStore
from hcuopt.adapters.noop_builder import NoopBuilder
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.contracts.platform_v1 import SourceSnapshot, TargetSpec
from hcuopt.deployment.bw20_environment import build_probe_plan
from hcuopt.deployment.bw20_source_build import clone_locked_source, git, write_json
from hcuopt.source_hash import file_uri_to_path
from hcuopt.targets import target_fingerprint
from hcuopt.workers.handlers import JobHandlers

PROFILE = "bw20-framework-smoke-v1"
PARENT = Path("/home/github/hcu-auto-opt-runtime/bw20-api-source")
SHARED_SOURCE = "/home/github/hcu-auto-opt-runtime/sglang-das"
SOURCE_COMMIT = "dad582f28458cd0e11e0be675fbe7fcc7ab65ac1"


def validate_root(root: Path, task_id: str) -> Path:
    canonical_id = str(UUID(task_id))
    root = root.absolute()
    if (task_id != canonical_id or root != PARENT / canonical_id
            or root.resolve(strict=True) != root or not root.is_dir()):
        raise ValueError("source Worker requires its canonical per-task root")
    return root


def validate_target(target: TargetSpec) -> None:
    build_probe_plan(target)
    if (target.source_baseline.clean_checkout != SHARED_SOURCE
            or target.source_baseline.commit != SOURCE_COMMIT):
        raise ValueError("source Worker cannot change the shared source mount")


class IsolatedBW20SourceManager(GitSourceManager):
    def __init__(self, root: Path):
        super().__init__(PROFILE)
        self.root = root

    def prepare_baseline(self, target, output_dir):
        validate_target(target)
        isolated = self.root / "baseline"
        if isolated.exists() or isolated.is_symlink():
            raise ValueError("baseline already exists; reconcile before retrying preparation")
        clone_locked_source(target, isolated)
        scoped = target.model_copy(deep=True)
        scoped.source_baseline.clean_checkout = str(isolated)
        return super().prepare_baseline(scoped, output_dir)

    def _bound_baseline(self, baseline):
        if (baseline.worktree_uri != (self.root / "baseline").as_uri()
                or baseline.kind != "baseline" or not baseline.clean):
            raise ValueError("baseline snapshot is outside this task")
        path = file_uri_to_path(baseline.worktree_uri)
        if path.resolve(strict=True) != path:
            raise ValueError("baseline path is redirected")

    def create_candidate(self, baseline, candidate_id, output_dir):
        self._bound_baseline(baseline)
        return super().create_candidate(baseline, candidate_id, output_dir)

    def remove_candidate(self, baseline, candidate, output_dir):
        self._bound_baseline(baseline)
        return super().remove_candidate(baseline, candidate, output_dir)


def run_job(envelope: dict, root: Path) -> dict:
    job_type = envelope["job_type"]
    if job_type not in {"source_prepare", "noop_build"}:
        raise ValueError("CPU source Worker accepts only source_prepare/noop_build")
    payload = dict(envelope["payload"])
    root = validate_root(root, payload["task_id"])
    job_id = str(UUID(envelope["job_id"]))
    receipt = root / f"job-{job_id}.json"
    if receipt.exists() or receipt.is_symlink():
        raise ValueError("job receipt already exists; do not replay execution")
    target_path = root / "target.json"
    if job_type == "source_prepare":
        target = TargetSpec.model_validate(payload["target"])
        validate_target(target)
        if ("target_fingerprint" in payload
                and payload["target_fingerprint"] != target_fingerprint(target)):
            raise ValueError("source task fingerprint mismatch")
        if target_path.exists() or target_path.is_symlink():
            raise ValueError("source task is already initialized")
        write_json(target_path, target.model_dump(mode="json"))
    else:
        if target_path.resolve(strict=True) != target_path:
            raise ValueError("redirected task target")
        target = TargetSpec.model_validate_json(target_path.read_bytes())
        validate_target(target)
        baseline = SourceSnapshot.model_validate(payload["baseline_source"])
        if (baseline.commit != target.source_baseline.commit
                or baseline.repository != target.source_baseline.repository):
            raise ValueError("build source differs from locked target")
    work = root / "work"
    work.mkdir(exist_ok=True)
    if work.resolve(strict=True) != work:
        raise ValueError("redirected Worker output")
    store_path = root / "store"
    if store_path.resolve(strict=False) != store_path:
        raise ValueError("redirected Artifact Store")
    registry = AdapterRegistry(
        profile=PROFILE, source_manager=IsolatedBW20SourceManager(root),
        builder=NoopBuilder(PROFILE), artifact_store=LocalArtifactStore(store_path, PROFILE),
    )
    shared = Path(target.source_baseline.clean_checkout)
    before = {"commit": git(shared, "rev-parse", "HEAD"),
              "status": git(shared, "status", "--porcelain"),
              "origin": git(shared, "config", "--get", "remote.origin.url")}
    if before["commit"] != target.source_baseline.commit or before["status"]:
        raise ValueError("shared source is no longer the clean locked commit")
    # No new source/build protocol: return the unmodified existing result contract.
    result = JobHandlers(registry, work).handle(job_type, payload)
    after = {"commit": git(shared, "rev-parse", "HEAD"),
             "status": git(shared, "status", "--porcelain"),
             "origin": git(shared, "config", "--get", "remote.origin.url")}
    if before != after:
        raise ValueError("shared source changed during job")
    record = {"job_id": job_id, "task_id": payload["task_id"], "job_type": job_type,
              "target_fingerprint": target_fingerprint(target),
              "profile": PROFILE, "result": result, "shared_before": before,
              "shared_after": after, "hcu_used": False, "automatic_release_allowed": False}
    write_json(receipt, record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--task-root", type=Path, required=True)
    args = parser.parse_args()
    if args.request.resolve(strict=True) != args.request.absolute():
        raise ValueError("request path is redirected")
    print(json.dumps(run_job(json.loads(args.request.read_text()), args.task_root), indent=2))


if __name__ == "__main__":
    main()
