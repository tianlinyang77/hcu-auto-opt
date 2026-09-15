# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Trusted deployment guards, not a registered profile or a hardware grant."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from uuid import UUID

from hcuopt.deployment.bw20_stage0_runtime import ROOT, BW20TimingPlan, capture_process_binding
from hcuopt.deployment.bw20_timing_session import BW20TimingSession

# Executed read-only by the existing host Python 3.6. Never imports controller code.
VERIFY_SOURCE = r'''
import hashlib,json,os,pathlib,re,stat,sys
root=pathlib.Path(sys.argv[1])
pin=sys.argv[2]
if root.resolve(strict=True)!=root or not root.is_dir():
    raise ValueError('redirected controller root')
def read_file(path, limit):
    before=path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>limit:
        raise ValueError('nonregular or oversized source')
    flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)
    with os.fdopen(os.open(str(path),flags),'rb') as f:
        opened=os.fstat(f.fileno())
        if (opened.st_dev,opened.st_ino)!=(before.st_dev,before.st_ino):
            raise ValueError('source replaced during open')
        data=f.read(limit+1)
        after=os.fstat(f.fileno())
    if len(data)>limit or (after.st_size,after.st_mtime_ns)!=(before.st_size,before.st_mtime_ns):
        raise ValueError('source changed during read')
    return data
def unique(pairs):
    out={}
    for k,v in pairs:
        if k in out: raise ValueError('duplicate manifest member')
        out[k]=v
    return out
p=root/'controller-manifest.json'
raw=read_file(p,1048576)
if 'sha256:'+hashlib.sha256(raw).hexdigest()!=pin:
    raise ValueError('manifest differs from independent pin')
manifest=json.loads(raw.decode('utf-8'),object_pairs_hook=unique)
if not isinstance(manifest,dict) or not 1<=len(manifest)<=4096:
    raise ValueError('invalid manifest')
allowed_dirs=set()
for name,h in manifest.items():
    rel=pathlib.PurePosixPath(name)
    if (not name or '\\' in name or rel.is_absolute() or str(rel)!=name
            or '..' in rel.parts or name==p.name
            or not isinstance(h,str) or re.fullmatch('[0-9a-f]{64}',h) is None):
        raise ValueError('invalid source member')
    allowed_dirs.update(str(x) for x in rel.parents if str(x)!='.')
seen=set(); size=0
for parent,dirs,files in os.walk(str(root),followlinks=False):
    for name in dirs:
        d=pathlib.Path(parent)/name
        if d.is_symlink() or d.relative_to(root).as_posix() not in allowed_dirs:
            raise ValueError('unexpected source directory')
    for name in files:
        f=pathlib.Path(parent)/name
        rel=f.relative_to(root).as_posix()
        if rel==p.name: continue
        if rel not in manifest: raise ValueError('unexpected source file')
        data=read_file(f,4194304); size+=len(data)
        if size>67108864: raise ValueError('source budget exceeded')
        if hashlib.sha256(data).hexdigest()!=manifest[rel]:
            raise ValueError('source hash mismatch')
        seen.add(rel)
if seen!=set(manifest) or read_file(p,1048576)!=raw:
    raise ValueError('incomplete or changed inventory')
print(json.dumps(dict(schema_version='bw20-controller-check-v1',root=str(root),
                     manifest_sha256=pin,file_count=len(seen),total_bytes=size)))
'''


class BW20SourceGuard:
    """Verify a previously staged controller against a deployment-pinned manifest.

    This is a bounded snapshot check, not a concurrent-writer exclusion mechanism.
    Deployment must protect the source root until all sessions have terminated.
    """

    def __init__(self, *, runner, source_root: str, manifest_sha256: str):
        if (runner.host, runner.user, runner.port) != ("10.17.1.20", "github", 22):
            raise ValueError("source guard requires BW20 endpoint")
        root = PurePosixPath(source_root)
        if (root.name != "controller" or str(root.parent.parent) != ROOT
                or str(UUID(root.parent.name)) != root.parent.name or str(root) != source_root):
            raise ValueError("invalid staged controller path")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_sha256) is None:
            raise ValueError("independently pinned manifest hash required")
        self.runner, self.source_root, self.pin = runner, source_root, manifest_sha256
        self.observations = []

    def __call__(self, plan: BW20TimingPlan) -> None:
        if plan.source_root != self.source_root:
            raise ValueError("staging does not belong to this session")
        result = self.runner.run(
            ("python3", "-c", VERIFY_SOURCE, self.source_root, self.pin), timeout=20,
        )
        if result.returncode or len(result.stdout) > 8192 or len(result.stderr) > 8192:
            raise RuntimeError("staged controller verification failed")
        record = json.loads(result.stdout)
        if (record.get("schema_version") != "bw20-controller-check-v1"
                or record.get("root") != self.source_root
                or record.get("manifest_sha256") != self.pin
                or type(record.get("file_count")) is not int
                or not 1 <= record["file_count"] <= 4096
                or type(record.get("total_bytes")) is not int
                or not 0 <= record["total_bytes"] <= 67108864):
            raise RuntimeError("invalid source verification receipt")
        self.observations.append(record)


def guarded_job_session(*, plan, transport, source_guard, context,
                        cancelled, budget_seconds=480):
    """Wire original Worker capabilities; do not synthesize a lease from JSON fields."""
    guard = context.get("assert_live_lease")
    if (context.get("resource_id") != plan.resource_id
            or type(context.get("fencing_token")) is not int
            or context["fencing_token"] != plan.fencing_token
            or context.get("lease_scope") != "exclusive" or not callable(guard)):
        raise ValueError("trusted exclusive Worker lease context required")
    UUID(str(context["lease_id"]))
    UUID(str(context["job_id"]))
    return BW20TimingSession(
        plan=plan, transport=transport, assert_staging=source_guard,
        assert_lease=guard, cancelled=cancelled, budget_seconds=budget_seconds,
        bind_process=lambda cid, ready: capture_process_binding(
            plan=plan, container_id=cid, ready=ready,
            inspect=lambda key: transport.inspect(key, 10), proc_root=PurePosixPath("/proc"),
            read_proc=transport.read_proc, read_namespace=transport.read_namespace),
    )
