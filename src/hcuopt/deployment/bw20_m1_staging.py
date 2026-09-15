# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Stage bounded BW20 M1 controller/output inputs without starting HCU work."""

from __future__ import annotations

import ast
import hashlib
import json
import stat
from dataclasses import dataclass
from pathlib import Path

from hcuopt.deployment.bw20_m1_runtime import ROOT, BW20M1ContainerPlan
from hcuopt.deployment.bw20_pair_bridge import BW20Transport
from hcuopt.deployment.bw20_stage0_guards import BW20SourceGuard
from hcuopt.deployment.bw20_stage0_staging import ControllerBundle, stage_controller

INIT_OUTPUT = r"""
import json,pathlib,sys,uuid
root=pathlib.Path('/home/github/hcu-auto-opt-runtime/bw20-m1')
if root.exists() and root.resolve(strict=True)!=root: raise ValueError('redirected M1 root')
root.mkdir(mode=0o700,exist_ok=True)
run=pathlib.Path(sys.argv[1])
if run.parent!=root or str(uuid.UUID(run.name))!=run.name: raise ValueError('invalid M1 run')
run.mkdir(mode=0o700)
for name in ('evidence','cache'):
 (run/name).mkdir(mode=0o700)
print(json.dumps(dict(schema_version='bw20-m1-output-init-v1',run=str(run),
                      evidence=str(run/'evidence'),cache=str(run/'cache'))))
"""

VERIFY_OUTPUT = r"""
import hashlib,json,pathlib,stat,sys,uuid
run=pathlib.Path(sys.argv[1]); arm=sys.argv[2]; expected=sys.argv[3]
root=pathlib.Path('/home/github/hcu-auto-opt-runtime/bw20-m1')
if (run.parent!=root or str(uuid.UUID(run.name))!=run.name
        or run.resolve(strict=True)!=run or run.is_symlink()):
 raise ValueError('invalid M1 output root')
for name in ('evidence','cache'):
 p=run/name
 if p.resolve(strict=True)!=p or p.is_symlink() or not p.is_dir() or any(p.iterdir()):
  raise ValueError('M1 output directory is not fresh')
p=run/'candidate-overlay.py'
if arm=='candidate':
 before=p.lstat()
 if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>262144:
  raise ValueError('invalid Candidate overlay')
 h=hashlib.sha256()
 with p.open('rb') as f:
  for block in iter(lambda:f.read(1048576),b''): h.update(block)
 if 'sha256:'+h.hexdigest()!=expected or p.lstat()!=before:
  raise ValueError('Candidate overlay changed or differs from pin')
elif arm!='baseline' or expected!='none' or p.exists() or p.is_symlink():
 raise ValueError('unexpected Baseline/Candidate staging state')
print(json.dumps(dict(schema_version='bw20-m1-staging-v1',run=str(run),arm=arm,
                      artifact_hash=None if expected=='none' else expected,
                      evidence_empty=True,cache_empty=True)))
"""


@dataclass(frozen=True, slots=True)
class BW20M1StagedRun:
    source_guard: BW20SourceGuard
    receipt: dict[str, object]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _artifact(path: Path | None, plan: BW20M1ContainerPlan) -> Path | None:
    if plan.arm == "baseline":
        if path is not None or plan.artifact_path is not None or plan.artifact_hash is not None:
            raise ValueError("Baseline M1 staging cannot accept a Candidate Artifact")
        return None
    if path is None or plan.artifact_path is None or plan.artifact_hash is None:
        raise ValueError("Candidate M1 staging requires one Artifact")
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 256_000:
        raise ValueError("Candidate M1 Artifact must be a bounded regular file")
    if _sha256(path) != plan.artifact_hash or path.lstat() != info:
        raise ValueError("Candidate M1 Artifact differs from its frozen Hash")
    return path


def stage_m1_run(
    *,
    plan: BW20M1ContainerPlan,
    bundle: ControllerBundle,
    runner,
    artifact: Path | None = None,
) -> BW20M1StagedRun:
    """Publish immutable inputs and fresh output directories for one M1 process."""

    selected_artifact = _artifact(artifact, plan)
    source_guard = stage_controller(plan=plan, bundle=bundle, runner=runner)
    transport = BW20Transport(runner)
    run_root = str(ROOT / str(plan.run_id))
    raw = transport.checked(("python3", "-c", INIT_OUTPUT, run_root), timeout=15)
    initialized = json.loads(raw)
    if (
        initialized.get("schema_version") != "bw20-m1-output-init-v1"
        or initialized.get("run") != run_root
        or initialized.get("evidence") != plan.evidence_root
        or initialized.get("cache") != plan.cache_root
    ):
        raise RuntimeError("BW20 M1 output initialization receipt mismatch")
    for output in (plan.evidence_root, plan.cache_root):
        transport.checked(("setfacl", "-m", "u:65534:rwx", output), timeout=10)
        acl = transport.checked(("getfacl", "-cpn", output), timeout=10).decode("utf-8")
        if "user:65534:rwx" not in acl.splitlines():
            raise RuntimeError("BW20 M1 output ACL was not applied")
    if selected_artifact is not None:
        transport.copy(selected_artifact, plan.artifact_path)
        transport.checked(("chmod", "0444", plan.artifact_path), timeout=10)
    expected = plan.artifact_hash or "none"
    verified = json.loads(
        transport.checked(
            ("python3", "-c", VERIFY_OUTPUT, run_root, plan.arm, expected),
            timeout=15,
        )
    )
    if (
        verified.get("schema_version") != "bw20-m1-staging-v1"
        or verified.get("run") != run_root
        or verified.get("arm") != plan.arm
        or verified.get("artifact_hash") != plan.artifact_hash
        or verified.get("evidence_empty") is not True
        or verified.get("cache_empty") is not True
    ):
        raise RuntimeError("BW20 M1 staging verification receipt mismatch")
    source_guard(plan)
    return BW20M1StagedRun(source_guard=source_guard, receipt=verified)


def host_scripts_are_python36_compatible() -> bool:
    ast.parse(INIT_OUTPUT, feature_version=(3, 6))
    ast.parse(VERIFY_OUTPUT, feature_version=(3, 6))
    return True


__all__ = [
    "BW20M1StagedRun",
    "INIT_OUTPUT",
    "VERIFY_OUTPUT",
    "host_scripts_are_python36_compatible",
    "stage_m1_run",
]
