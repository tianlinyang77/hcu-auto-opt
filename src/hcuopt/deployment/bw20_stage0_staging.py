# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Bounded controller upload, no HCU execution or automatic profile registration.

Permissions protect against accidental writes, not the owning host UID or root.
Failures retain their UUID directory for reconciliation; never blindly retry it.
"""

from __future__ import annotations

import hashlib
import io
import json
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from hcuopt.deployment.bw20_pair_bridge import BW20Transport
from hcuopt.deployment.bw20_stage0_guards import BW20SourceGuard
from hcuopt.deployment.bw20_stage0_runtime import ROOT, BW20TimingPlan

IDENTITY_ASSETS = {
    "src/hcuopt/deployment/assets/bw20-passwd": (
        b"root:x:0:0:root:/root:/bin/sh\n"
        b"github:x:1002:1002:BW20 Stage0:/tmp:/usr/sbin/nologin\n"
        b"nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"
    ),
    "src/hcuopt/deployment/assets/bw20-group": (
        b"root:x:0:\n"
        b"github:x:1002:\n"
        b"nogroup:x:65534:\n"
    ),
}

INIT_UPLOAD = r'''
import pathlib,sys,uuid
parent=pathlib.Path('/home/github/hcu-auto-opt-runtime')
if parent.resolve(strict=True)!=parent: raise ValueError('redirected runtime parent')
root=parent/'bw20-stage0'
if root.exists() and root.resolve(strict=True)!=root: raise ValueError('redirected staging root')
root.mkdir(mode=0o700,exist_ok=True)
run=pathlib.Path(sys.argv[1])
if run.parent!=root or str(uuid.UUID(run.name))!=run.name: raise ValueError('invalid run root')
run.mkdir(mode=0o700)
'''

UNPACK_UPLOAD = r'''
import hashlib,json,os,pathlib,re,stat,sys,tarfile,uuid
run=pathlib.Path(sys.argv[1]); archive_pin=sys.argv[2]; manifest_pin=sys.argv[3]
if (run.parent!=pathlib.Path('/home/github/hcu-auto-opt-runtime/bw20-stage0')
        or str(uuid.UUID(run.name))!=run.name or run.resolve(strict=True)!=run):
    raise ValueError('invalid upload root')
archive=run/'controller.tar'
if archive.is_symlink() or not archive.is_file() or archive.stat().st_size>83886080:
    raise ValueError('invalid upload')
with archive.open('rb') as stream:
    digest=hashlib.sha256()
    for block in iter(lambda:stream.read(1048576),b''): digest.update(block)
if 'sha256:'+digest.hexdigest()!=archive_pin: raise ValueError('uploaded archive hash mismatch')
dest=run/'controller'
dest.mkdir(mode=0o700)
seen=set(); total=0
with tarfile.open(str(archive),'r:') as tar:
    for member in tar:
        rel=pathlib.PurePosixPath(member.name)
        if (not member.isfile() or rel.is_absolute() or '..' in rel.parts
                or str(rel)!=member.name or '\\' in member.name or member.name in seen
                or not (member.name=='controller-manifest.json'
                        or (member.name.startswith('src/') and member.name.endswith('.py'))
                        or (str(rel.parent)=='src/hcuopt/evaluation/protocols'
                            and member.name.endswith('.yaml'))
                        or (str(rel.parent)=='src/hcuopt/storage/sql'
                            and member.name.endswith('.sql'))
                        or member.name in (
                            'src/hcuopt/deployment/assets/bw20-passwd',
                            'src/hcuopt/deployment/assets/bw20-group'))
                or member.size<0 or member.size>4194304 or len(seen)>=4097):
            raise ValueError('unsafe controller archive member')
        total+=member.size
        if total>68157440: raise ValueError('controller archive too large')
        target=dest/member.name
        target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        with tar.extractfile(member) as source, target.open('xb') as output:
            remaining=member.size
            while remaining:
                block=source.read(min(1048576,remaining))
                if not block: raise ValueError('short archive member')
                output.write(block); remaining-=len(block)
        target.chmod(0o444)
        seen.add(member.name)
p=dest/'controller-manifest.json'
raw=p.read_bytes()
if 'sha256:'+hashlib.sha256(raw).hexdigest()!=manifest_pin:
    raise ValueError('manifest pin mismatch')
manifest=json.loads(raw.decode('utf-8'))
if set(manifest)|{p.name}!=seen: raise ValueError('archive inventory differs')
for name,expected in manifest.items():
    if hashlib.sha256((dest/name).read_bytes()).hexdigest()!=expected:
        raise ValueError('extracted source mismatch')
for parent,dirs,files in os.walk(str(dest),topdown=False):
    pathlib.Path(parent).chmod(0o555)
archive.chmod(0o444)
print(json.dumps(dict(root=str(dest),manifest_sha256=manifest_pin,
                     archive_sha256=archive_pin,file_count=len(manifest),
                     protection='readonly_modes_not_owner_uid_isolation')))
'''


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ControllerBundle:
    archive: Path
    archive_sha256: str
    manifest_sha256: str
    file_count: int


def freeze_controller(repository: Path, archive: Path) -> ControllerBundle:
    """Freeze Python and required protocol/SQL resources, not credentials or local state."""
    root = repository.absolute()
    if root.resolve(strict=True) != root:
        raise ValueError("redirected repository")
    members = {}
    total = 0
    sources = list((root / "src").rglob("*.py"))
    sources.extend((root / "src/hcuopt/evaluation/protocols").glob("*.yaml"))
    sources.extend((root / "src/hcuopt/storage/sql").glob("*.sql"))
    sources.extend(root / name for name in IDENTITY_ASSETS)
    for source in sorted(sources):
        info = source.lstat()
        if (source.resolve(strict=True) != source or not stat.S_ISREG(info.st_mode)
                or info.st_size > 4194304):
            raise ValueError("unsafe controller source")
        raw = source.read_bytes()
        total += len(raw)
        if total > 67108864 or len(members) >= 4096:
            raise ValueError("controller source budget exceeded")
        if source.read_bytes() != raw:
            raise ValueError("controller changed while freezing")
        members[source.relative_to(root).as_posix()] = raw
    if any(members.get(name) != expected for name, expected in IDENTITY_ASSETS.items()):
        raise ValueError("BW20 controller identity assets differ from the approved content")
    if "src/hcuopt/deployment/bw20_stage0_worker.py" not in members:
        raise ValueError("BW20 controller entrypoint missing")
    manifest = json.dumps({name: hashlib.sha256(raw).hexdigest() for name, raw in members.items()},
                          sort_keys=True, separators=(",", ":")).encode()
    members["controller-manifest.json"] = manifest
    with archive.open("xb") as stream, tarfile.open(fileobj=stream, mode="w:") as tar:
        for name, data in sorted(members.items()):
            item = tarfile.TarInfo(name)
            item.size, item.mode, item.mtime = len(data), 0o444, 0
            tar.addfile(item, io.BytesIO(data))
    return ControllerBundle(archive, sha(archive.read_bytes()), sha(manifest), len(members) - 1)


def stage_controller(*, plan: BW20TimingPlan, bundle: ControllerBundle, runner) -> BW20SourceGuard:
    """Upload to a fresh canonical UUID directory, then independently re-read it."""
    guard = BW20SourceGuard(runner=runner, source_root=plan.source_root,
                            manifest_sha256=bundle.manifest_sha256)
    run = plan.source_root.removesuffix("/controller")
    if run != ROOT + "/" + str(UUID(run.rsplit("/", 1)[-1])):
        raise ValueError("invalid staging run")
    if sha(bundle.archive.read_bytes()) != bundle.archive_sha256:
        raise ValueError("local controller archive changed")
    transport = BW20Transport(runner)
    transport.checked(("python3", "-c", INIT_UPLOAD, run), timeout=15)
    from hcuopt.deployment.bw20_local_runner import BW20LocalCommandRunner

    if isinstance(runner, BW20LocalCommandRunner):
        runner.copy_controller_archive(bundle.archive, run + "/controller.tar")
    else:
        transport.copy(bundle.archive, run + "/controller.tar")
    raw = transport.checked(("python3", "-c", UNPACK_UPLOAD, run,
                             bundle.archive_sha256, bundle.manifest_sha256), timeout=30)
    receipt = json.loads(raw)
    if (receipt.get("root") != plan.source_root
            or receipt.get("archive_sha256") != bundle.archive_sha256
            or receipt.get("manifest_sha256") != bundle.manifest_sha256
            or receipt.get("file_count") != bundle.file_count):
        raise RuntimeError("controller upload receipt mismatch")
    guard(plan)
    return guard
