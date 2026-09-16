# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Prepare and stage one bounded BW20 provisional endpoint ABBA group.

This module only freezes inputs and proves the remote staging layout.  It does
not acquire a Lease, start a container, access HCU, or produce a performance
verdict.
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from hcuopt.adapters.bw20_endpoint_execution import (
    BASELINE_MODULE_HASH,
    CANDIDATE_MODULE_HASH,
    ENDPOINT_ARGV,
    PROFILE,
    RUN_PARENT,
    SIGNED_ARTIFACT_PATH,
    TARGET_MODULE_NAME,
    TARGET_MODULE_PATH,
    BW20EndpointExecutionAdapter,
    endpoint_mounts,
)
from hcuopt.adapters.bw20_execution import RESOURCE_ID
from hcuopt.contracts.platform_v1 import ExecutionRequest, TargetSpec
from hcuopt.deployment.bw20_local_runner import BW20LocalCommandRunner
from hcuopt.deployment.bw20_pair_bridge import BW20Transport
from hcuopt.deployment.bw20_smoke_preflight import MODEL, MODEL_HASHES
from hcuopt.domain.enums import LeaseScope
from hcuopt.evaluation.endpoint_workload import load_endpoint_workload_spec
from hcuopt.evaluation.sglang_smoke import load_workload_spec

ACQUISITION_ORDER = ("baseline", "candidate", "candidate", "baseline")
SCHEMA_VERSION = "bw20-endpoint-staging-plan-v1"
INPUT_SOURCES = {
    "src/hcuopt/evaluation/sglang_endpoint_runner.py": Path(
        "src/hcuopt/evaluation/sglang_endpoint_runner.py"
    ),
    "src/hcuopt/evaluation/sglang_smoke_runner.py": Path(
        "src/hcuopt/evaluation/sglang_smoke_runner.py"
    ),
    "activation/sitecustomize.py": Path(
        "src/hcuopt/evaluation/endpoint_sitecustomize.py"
    ),
}

INIT_RUN = r"""
import pathlib,sys,uuid
parent=pathlib.Path('/home/github/hcu-auto-opt-runtime')
if parent.resolve(strict=True)!=parent: raise ValueError('redirected runtime parent')
root=parent/'bw20-endpoint-validation'
if root.exists() and root.resolve(strict=True)!=root: raise ValueError('redirected endpoint root')
root.mkdir(mode=0o700,exist_ok=True)
run=pathlib.Path(sys.argv[1])
if run.parent!=root or str(uuid.UUID(run.name))!=run.name: raise ValueError('invalid endpoint run')
run.mkdir(mode=0o700)
for name in ('input','input/src','input/src/hcuopt','input/src/hcuopt/evaluation',
             'input/activation','acquisitions'):
 (run/name).mkdir(mode=0o700)
for name in ('0000-baseline','0001-candidate','0002-candidate','0003-baseline'):
 (run/'acquisitions'/name).mkdir(mode=0o700)
"""

VERIFY_RUN = r"""
import hashlib,json,os,pathlib,stat,sys,uuid
run=pathlib.Path(sys.argv[1]); plan_pin=sys.argv[2]
artifact_path=pathlib.Path(sys.argv[3]); artifact_pin=sys.argv[4]
model_root=pathlib.Path(sys.argv[5])
root=pathlib.Path('/home/github/hcu-auto-opt-runtime/bw20-endpoint-validation')
if (run.parent!=root or str(uuid.UUID(run.name))!=run.name
        or run.resolve(strict=True)!=run or run.is_symlink()):
 raise ValueError('invalid endpoint run root')
def read_file(path,limit):
 before=path.lstat()
 if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
       or before.st_nlink!=1 or before.st_size<1 or before.st_size>limit):
  raise ValueError('unsafe staged file')
 flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)
 with os.fdopen(os.open(str(path),flags),'rb') as stream:
  opened=os.fstat(stream.fileno())
  if (opened.st_dev,opened.st_ino)!=(before.st_dev,before.st_ino):
   raise ValueError('staged file changed during open')
  data=stream.read(limit+1); after=os.fstat(stream.fileno())
 if (len(data)>limit or (after.st_size,after.st_mtime_ns)!=(before.st_size,before.st_mtime_ns)):
  raise ValueError('staged file changed during read')
 return data,before
def hash_file(path,limit):
 before=path.lstat()
 if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
       or before.st_nlink!=1 or before.st_size<1 or before.st_size>limit):
  raise ValueError('unsafe staged file')
 flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)
 digest=hashlib.sha256()
 with os.fdopen(os.open(str(path),flags),'rb') as stream:
  opened=os.fstat(stream.fileno())
  if (opened.st_dev,opened.st_ino)!=(before.st_dev,before.st_ino):
   raise ValueError('staged file changed during open')
  for block in iter(lambda:stream.read(1048576),b''): digest.update(block)
  after=os.fstat(stream.fileno())
 if (after.st_size,after.st_mtime_ns)!=(before.st_size,before.st_mtime_ns):
  raise ValueError('staged file changed during read')
 return digest.hexdigest(),before
raw,_=read_file(run/'plan.json',1048576)
if 'sha256:'+hashlib.sha256(raw).hexdigest()!=plan_pin:
 raise ValueError('endpoint plan hash mismatch')
plan=json.loads(raw.decode('utf-8'))
if (plan.get('schema_version')!='bw20-endpoint-staging-plan-v1'
        or plan.get('remote_run_root')!=str(run)
        or plan.get('candidate_artifact_path')!=str(artifact_path)
        or plan.get('candidate_artifact_sha256')!=artifact_pin
        or plan.get('model_root')!=str(model_root)):
 raise ValueError('endpoint plan identity mismatch')
expected=plan.get('input_sha256')
if not isinstance(expected,dict) or not expected:
 raise ValueError('endpoint input manifest missing')
seen=set(); actual={}; total=0; input_root=run/'input'
for parent,dirs,files in os.walk(str(input_root),followlinks=False):
 for name in dirs:
  if (pathlib.Path(parent)/name).is_symlink(): raise ValueError('redirected input directory')
 for name in files:
  path=pathlib.Path(parent)/name; rel=path.relative_to(input_root).as_posix()
  if rel not in expected: raise ValueError('unexpected endpoint input')
  data,info=read_file(path,4194304); total+=len(data)
  value='sha256:'+hashlib.sha256(data).hexdigest()
  if value!=expected[rel] or stat.S_IMODE(info.st_mode)&0o222:
   raise ValueError('endpoint input hash or mode mismatch')
  actual[rel]=value; seen.add(rel)
if seen!=set(expected) or total>16777216:
 raise ValueError('endpoint input inventory mismatch')
artifact,artifact_info=read_file(artifact_path,16777216)
if ('sha256:'+hashlib.sha256(artifact).hexdigest()!=artifact_pin
        or stat.S_IMODE(artifact_info.st_mode)&0o222):
 raise ValueError('Candidate Artifact hash or mode mismatch')
models=plan.get('expected_model_sha256')
if not isinstance(models,dict) or len(models)!=8:
 raise ValueError('endpoint model manifest differs')
observed_models={}
for name,pin in models.items():
 path=model_root/name
 value,info=hash_file(path,2147483648)
 if stat.S_IMODE(info.st_mode)&0o222: raise ValueError('model file is writable')
 if value!=pin: raise ValueError('model hash mismatch: '+name)
 observed_models[name]=value
names=('0000-baseline','0001-candidate','0002-candidate','0003-baseline')
for name in names:
 path=run/'acquisitions'/name
 if (path.resolve(strict=True)!=path or path.is_symlink() or not path.is_dir()
       or any(path.iterdir())):
  raise ValueError('endpoint acquisition output is not fresh')
print(json.dumps(dict(schema_version='bw20-endpoint-staging-receipt-v1',
 run=str(run),plan_sha256=plan_pin,input_sha256=actual,
 candidate_artifact_sha256=artifact_pin,model_hashes=observed_models,
 acquisition_outputs=list(names),output_acl_uid=65534)))
"""


@dataclass(frozen=True, slots=True)
class PreparedEndpointRun:
    directory: Path
    remote_run_root: str
    plan_sha256: str
    requests: tuple[ExecutionRequest, ...]


@dataclass(frozen=True, slots=True)
class StagedEndpointRun:
    receipt: dict[str, Any]
    prepared: PreparedEndpointRun


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _regular_file(path: Path, *, limit: int = 16 * 1024 * 1024) -> Path:
    path = path.absolute()
    before = path.lstat()
    if (
        path.resolve(strict=True) != path
        or path.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not 0 < before.st_size <= limit
    ):
        raise ValueError(f"unsafe endpoint input: {path}")
    return path


def _write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, allow_nan=False, indent=2, sort_keys=True)
        stream.write("\n")
    path.chmod(0o444)


def _copy_verified(source: Path, destination: Path) -> str:
    source = _regular_file(source)
    before = source.lstat()
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    digest = _sha256(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
    after = source.lstat()
    if _sha256(destination) != digest or (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) != identity:
        raise RuntimeError(f"endpoint input changed while copying: {source.name}")
    destination.chmod(0o444)
    return digest


def _acquisition_spec(
    *, endpoint: Any, smoke: Any, arm: str, ordinal: int,
    warmup_requests: int, measured_requests: int,
) -> dict[str, Any]:
    smoke_spec = smoke.model_dump(mode="json")
    smoke_spec.update(
        workload_id=endpoint.workload_id,
        target_id=endpoint.target_id,
        model_path=endpoint.model_path,
        served_model_name=endpoint.served_model_name,
        tensor_parallel_size=endpoint.tensor_parallel_size,
        prompt=endpoint.prompt,
        temperature=endpoint.temperature,
        max_new_tokens=endpoint.expected_completion_tokens,
        sampling_seed=endpoint.sampling_seed,
        stream=False,
        generate_path=endpoint.endpoint_path,
        attention_backend=endpoint.attention_backend,
        page_size=endpoint.page_size,
    )
    expected_hash = CANDIDATE_MODULE_HASH if arm == "candidate" else BASELINE_MODULE_HASH
    return {
        "protocol_version": "sglang-endpoint-acquisition-v1",
        "arm": arm,
        "acquisition_ordinal": ordinal,
        "warmup_requests": warmup_requests,
        "measured_requests": measured_requests,
        "expected_prompt_tokens": endpoint.expected_prompt_tokens,
        "expected_completion_tokens": endpoint.expected_completion_tokens,
        "ignore_eos": True,
        "activation_attestation": {
            "module_name": TARGET_MODULE_NAME,
            "module_path": TARGET_MODULE_PATH,
            "expected_sha256": expected_hash,
        },
        "smoke_spec": smoke_spec,
    }


def prepare_endpoint_run(
    *, repository: Path, destination: Path, run_id: UUID, fencing_token: int,
    target: TargetSpec, warmup_requests: int = 1, measured_requests: int = 2,
) -> PreparedEndpointRun:
    """Freeze one complete provisional ABBA group without remote or HCU access."""

    if not 1 <= warmup_requests <= 10_000 or not 1 <= measured_requests <= 100_000:
        raise ValueError("endpoint request budget is outside the protocol range")
    repository = repository.absolute()
    if repository.resolve(strict=True) != repository:
        raise ValueError("redirected endpoint repository")
    endpoint = load_endpoint_workload_spec(
        repository / "config/workloads/bw20-sglang-endpoint-provisional-v1.yaml"
    )
    smoke = load_workload_spec(repository / "config/workloads/bw20-sglang-smoke-v1.yaml")
    if (
        endpoint.target_id != target.target_id
        or endpoint.image_digest != target.inference_image.registry_digest
        or endpoint.source_commit != target.source_baseline.commit
        or endpoint.model_path != MODEL
    ):
        raise ValueError("endpoint workload differs from the BW20 Target Lock")
    remote_run_root = f"{RUN_PARENT}/{run_id}"
    policy = BW20EndpointExecutionAdapter(
        runner=_NoCommandRunner(), run_root=remote_run_root
    )
    destination = destination.absolute()
    if destination.parent.resolve(strict=True) != destination.parent:
        raise ValueError("endpoint bundle parent is redirected")
    destination.mkdir()
    inputs = destination / "input"
    inputs.mkdir()
    hashes: dict[str, str] = {}
    for relative, source in INPUT_SOURCES.items():
        hashes[relative] = _copy_verified(repository / source, inputs / relative)
    requests = []
    for ordinal, arm in enumerate(ACQUISITION_ORDER):
        name = f"{ordinal:04d}-{arm}-spec.json"
        spec = _acquisition_spec(
            endpoint=endpoint,
            smoke=smoke,
            arm=arm,
            ordinal=ordinal,
            warmup_requests=warmup_requests,
            measured_requests=measured_requests,
        )
        _write_json(inputs / name, spec)
        hashes[name] = _sha256(inputs / name)
        request = ExecutionRequest(
            target_id=target.target_id,
            argv=list(ENDPOINT_ARGV),
            working_directory="/work",
            timeout_seconds=600,
            lease_scope=LeaseScope.EXCLUSIVE,
            resource_id=RESOURCE_ID,
            fencing_token=fencing_token,
            container_image=target.inference_image.immutable_reference,
            mounts=endpoint_mounts(remote_run_root, arm, ordinal),
        )
        policy.validate_scope(request, target)
        requests.append(request)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "remote_run_root": remote_run_root,
        "profile": PROFILE,
        "run_mode": "provisional",
        "acquisition_order": list(ACQUISITION_ORDER),
        "warmup_requests": warmup_requests,
        "measured_requests": measured_requests,
        "input_sha256": dict(sorted(hashes.items())),
        "candidate_artifact_path": SIGNED_ARTIFACT_PATH,
        "candidate_artifact_sha256": CANDIDATE_MODULE_HASH,
        "model_root": MODEL,
        "expected_model_sha256": MODEL_HASHES,
        "requests": [request.model_dump(mode="json") for request in requests],
        "hcu_accessed": False,
        "producer_verdict": None,
        "automatic_release_allowed": False,
    }
    _write_json(destination / "plan.json", payload)
    return PreparedEndpointRun(
        directory=destination,
        remote_run_root=remote_run_root,
        plan_sha256=_sha256(destination / "plan.json"),
        requests=tuple(requests),
    )


def verify_prepared_endpoint_run(prepared: PreparedEndpointRun) -> dict[str, Any]:
    plan_path = _regular_file(prepared.directory / "plan.json", limit=1024 * 1024)
    if _sha256(plan_path) != prepared.plan_sha256:
        raise ValueError("prepared endpoint plan hash drift")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if (
        plan.get("schema_version") != SCHEMA_VERSION
        or plan.get("remote_run_root") != prepared.remote_run_root
        or plan.get("acquisition_order") != list(ACQUISITION_ORDER)
        or plan.get("candidate_artifact_path") != SIGNED_ARTIFACT_PATH
        or plan.get("candidate_artifact_sha256") != CANDIDATE_MODULE_HASH
        or plan.get("automatic_release_allowed") is not False
    ):
        raise ValueError("prepared endpoint plan identity drift")
    expected = plan.get("input_sha256")
    actual = {
        path.relative_to(prepared.directory / "input").as_posix(): _sha256(path)
        for path in sorted((prepared.directory / "input").rglob("*"))
        if path.is_file()
    }
    if actual != expected:
        raise ValueError("prepared endpoint input hash drift")
    requests = tuple(ExecutionRequest.model_validate(item) for item in plan["requests"])
    if requests != prepared.requests:
        raise ValueError("prepared endpoint requests drift")
    return plan


def _copy_to_remote(
    *, transport: BW20Transport, runner: Any, source: Path, destination: str
) -> None:
    if isinstance(runner, BW20LocalCommandRunner):
        target = Path(destination)
        with source.open("rb") as incoming, target.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
    else:
        transport.copy(source, destination)


def stage_endpoint_run(
    *, prepared: PreparedEndpointRun, runner: Any
) -> StagedEndpointRun:
    """Create fresh remote inputs/outputs and verify every mounted file before HCU use."""

    plan = verify_prepared_endpoint_run(prepared)
    transport = BW20Transport(runner)
    transport.checked(("python3", "-c", INIT_RUN, prepared.remote_run_root), timeout=15)
    for relative in [*sorted(plan["input_sha256"]), "../plan.json"]:
        if relative == "../plan.json":
            source = prepared.directory / "plan.json"
            destination = f"{prepared.remote_run_root}/plan.json"
        else:
            source = prepared.directory / "input" / relative
            destination = f"{prepared.remote_run_root}/input/{relative}"
        _copy_to_remote(
            transport=transport, runner=runner, source=source, destination=destination
        )
        transport.checked(("chmod", "0444", destination), timeout=10)
    for directory in (
        f"{prepared.remote_run_root}/input",
        f"{prepared.remote_run_root}/input/src",
        f"{prepared.remote_run_root}/input/src/hcuopt",
        f"{prepared.remote_run_root}/input/src/hcuopt/evaluation",
        f"{prepared.remote_run_root}/input/activation",
    ):
        transport.checked(("chmod", "0555", directory), timeout=10)
    for ordinal, arm in enumerate(ACQUISITION_ORDER):
        output = f"{prepared.remote_run_root}/acquisitions/{ordinal:04d}-{arm}"
        transport.checked(("setfacl", "-m", "u:65534:rwx", output), timeout=10)
        acl = transport.checked(("getfacl", "-cpn", output), timeout=10).decode("utf-8")
        if "user:65534:rwx" not in acl.splitlines():
            raise RuntimeError("endpoint acquisition output ACL was not applied")
    receipt = json.loads(
        transport.checked(
            (
                "python3",
                "-c",
                VERIFY_RUN,
                prepared.remote_run_root,
                prepared.plan_sha256,
                SIGNED_ARTIFACT_PATH,
                CANDIDATE_MODULE_HASH,
                MODEL,
            ),
            timeout=180,
        )
    )
    if (
        receipt.get("schema_version") != "bw20-endpoint-staging-receipt-v1"
        or receipt.get("run") != prepared.remote_run_root
        or receipt.get("plan_sha256") != prepared.plan_sha256
        or receipt.get("input_sha256") != plan["input_sha256"]
        or receipt.get("candidate_artifact_sha256") != CANDIDATE_MODULE_HASH
        or receipt.get("model_hashes") != MODEL_HASHES
        or receipt.get("acquisition_outputs")
        != [f"{index:04d}-{arm}" for index, arm in enumerate(ACQUISITION_ORDER)]
        or receipt.get("output_acl_uid") != 65534
    ):
        raise RuntimeError("BW20 endpoint staging receipt mismatch")
    return StagedEndpointRun(receipt=receipt, prepared=prepared)


def host_scripts_are_python36_compatible() -> bool:
    ast.parse(INIT_RUN, feature_version=(3, 6))
    ast.parse(VERIFY_RUN, feature_version=(3, 6))
    return True


class _NoCommandRunner:
    """Validation-only runner; policy validation must not execute commands."""

    def run(self, argv: list[str], timeout: float = 30.0) -> Any:
        raise AssertionError("endpoint prepare attempted command execution")


__all__ = [
    "ACQUISITION_ORDER",
    "INIT_RUN",
    "PreparedEndpointRun",
    "StagedEndpointRun",
    "VERIFY_RUN",
    "host_scripts_are_python36_compatible",
    "prepare_endpoint_run",
    "stage_endpoint_run",
    "verify_prepared_endpoint_run",
]
