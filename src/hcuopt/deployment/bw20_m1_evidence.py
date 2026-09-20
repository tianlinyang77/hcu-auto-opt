# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Content-addressed download of allowlisted BW20 M1 worker evidence."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path

from hcuopt.deployment.bw20_local_runner import BW20LocalCommandRunner
from hcuopt.deployment.bw20_m1_runtime import BW20M1ContainerPlan
from hcuopt.deployment.bw20_pair_bridge import BW20Transport
from hcuopt.measurement.models import RawEvidenceFileV2

READ_EVIDENCE = r"""
import hashlib,json,os,pathlib,re,stat,sys,uuid
p=pathlib.Path(sys.argv[1])
root=pathlib.Path('/home/github/hcu-auto-opt-runtime/bw20-m1')
run=p.parent.parent
allowed=(p.name in ('cache-namespace.json','import-attestation.json')
         or re.fullmatch(r'device-event-[0-9]{4}[.]json',p.name))
if (not allowed or run.parent!=root or str(uuid.UUID(run.name))!=run.name
        or p.parent!=run/'evidence' or run.resolve(strict=True)!=run
        or p.parent.resolve(strict=True)!=p.parent):
 raise ValueError('evidence path outside allowlist')
before=p.lstat()
if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>4194304:
 raise ValueError('invalid evidence file')
flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)
with os.fdopen(os.open(str(p),flags),'rb') as f:
 opened=os.fstat(f.fileno())
 if (opened.st_dev,opened.st_ino)!=(before.st_dev,before.st_ino):
  raise ValueError('evidence replaced during open')
 h=hashlib.sha256(); size=0
 for block in iter(lambda:f.read(1048576),b''):
  size+=len(block)
  if size>4194304: raise ValueError('oversized evidence')
  h.update(block)
 after=os.fstat(f.fileno())
if (after.st_size,after.st_mtime_ns)!=(before.st_size,before.st_mtime_ns):
 raise ValueError('evidence changed during read')
print(json.dumps(dict(schema_version='bw20-m1-evidence-read-v1',path=str(p),
                      size=size,sha256='sha256:'+h.hexdigest(),
                      inode=opened.st_ino,device=opened.st_dev,mtime_ns=after.st_mtime_ns)))
"""

ALLOWED_NAME = re.compile(
    r"(?:cache-namespace[.]json|import-attestation[.]json|device-event-[0-9]{4}[.]json)"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def copy_local_evidence(
    source: Path,
    destination: Path,
    receipt: dict[str, object],
) -> None:
    """Copy a no-follow immutable file when controller and HCU host are identical."""

    source, destination = source.absolute(), destination.absolute()
    before = source.lstat()
    if (
        source.resolve(strict=True) != source
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > 4 * 1024 * 1024
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (
            receipt.get("device"),
            receipt.get("inode"),
            receipt.get("size"),
            receipt.get("mtime_ns"),
        )
    ):
        raise RuntimeError("local BW20 M1 evidence differs from its read receipt")
    descriptor = os.open(
        source,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
    )
    with os.fdopen(descriptor, "rb") as reader:
        opened = os.fstat(reader.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError("local BW20 M1 evidence changed during open")
        with destination.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
        after = os.fstat(reader.fileno())
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise RuntimeError("local BW20 M1 evidence changed during copy")


class BW20M1EvidenceMirror:
    def __init__(self, *, plan: BW20M1ContainerPlan, runner, local_root: Path):
        self.plan = plan
        self.runner = runner
        self.transport = BW20Transport(runner)
        self.local_root = local_root.resolve()
        self.observations: list[dict[str, object]] = []

    def fetch(self, name: str, expected_hash: str) -> RawEvidenceFileV2:
        if ALLOWED_NAME.fullmatch(name) is None:
            raise ValueError("BW20 M1 evidence name is outside the allowlist")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", expected_hash) is None:
            raise ValueError("BW20 M1 evidence requires a SHA256 pin")
        remote = f"{self.plan.evidence_root}/{name}"
        before = self._read(remote)
        if before.get("sha256") != expected_hash:
            raise RuntimeError("remote BW20 M1 evidence differs from Worker receipt")
        self.local_root.mkdir(parents=True, exist_ok=True)
        destination = self.local_root / name
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("local BW20 M1 evidence destination already exists")
        if isinstance(self.runner, BW20LocalCommandRunner):
            copy_local_evidence(Path(remote), destination, before)
        else:
            self.transport.copy(remote, destination, download=True)
        if _sha256(destination) != expected_hash or destination.stat().st_size != before["size"]:
            raise RuntimeError("downloaded BW20 M1 evidence differs from its remote pin")
        after = self._read(remote)
        if after != before:
            raise RuntimeError("remote BW20 M1 evidence changed during download")
        record = {"before": before, "after": after, "local": str(destination)}
        self.observations.append(record)
        return RawEvidenceFileV2(
            uri=destination.resolve(strict=True).as_uri(),
            sha256=expected_hash,
        )

    def _read(self, remote: str) -> dict[str, object]:
        raw = self.transport.checked(("python3", "-c", READ_EVIDENCE, remote), timeout=15)
        value = json.loads(raw)
        if (
            value.get("schema_version") != "bw20-m1-evidence-read-v1"
            or value.get("path") != remote
            or type(value.get("size")) is not int
            or not 0 <= value["size"] <= 4 * 1024 * 1024
            or type(value.get("inode")) is not int
            or type(value.get("device")) is not int
            or type(value.get("mtime_ns")) is not int
        ):
            raise RuntimeError("invalid BW20 M1 evidence read receipt")
        return value


def host_script_is_python36_compatible() -> bool:
    ast.parse(READ_EVIDENCE, feature_version=(3, 6))
    return True


__all__ = [
    "BW20M1EvidenceMirror",
    "READ_EVIDENCE",
    "copy_local_evidence",
    "host_script_is_python36_compatible",
]
