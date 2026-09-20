# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Diagnostic BW20 transport boundary using the original Worker and D evaluator.

Not a production profile or API admission path. PostgreSQL owns the test job's
lease; it does not reserve hardware against unrelated host users. No signing or
performance authority is added here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path, PurePosixPath

from hcuopt.adapters.bw20_execution import RESOURCE_ID, validate_run_root
from hcuopt.adapters.execution import FencingGuard, OpenSSHCommandRunner
from hcuopt.adapters.resource_cleaner import ContainerResourceCleaner
from hcuopt.adapters.sglang_evaluator import SGLangSmokeEvaluator, SGLangSmokePlan
from hcuopt.contracts.platform_v1 import ExecutionRequest
from hcuopt.contracts.v1 import WorkerRegister
from hcuopt.deployment.bw20_pair_prepare import prepare_pair, verify_prepared_pair
from hcuopt.deployment.kernel_binary_inventory import file_hash
from hcuopt.domain.errors import ExecutionSafetyError, StaleFencingToken
from hcuopt.evaluation.sglang_smoke import load_workload_spec, normalize_response

PROFILE = "bw20-smoke-policy-unregistered-v1"
VARIANT_FILES = {
    "spec.json", "environment.json", "start.json", "server.log", "ready.jsonl",
    "request.json", "response.json", "stop.json", "result.json",
}

# Host Python is 3.6. This fixed program performs read-only, no-follow inventory.
INVENTORY = """
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1])
if str(root)!=str(root.resolve(strict=True)) or not root.is_dir():
    raise ValueError('invalid inventory root')
result={}
for p in sorted(root.iterdir()):
    if not p.is_file() or p.is_symlink():
        raise ValueError('nonregular inventory member')
    if p.stat().st_size > 134217728:
        raise ValueError('oversized evidence')
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''): h.update(b)
    result[p.name]='sha256:'+h.hexdigest()
print(json.dumps(result))
"""

HOST_STATE = """
import json,pathlib
p=pathlib.Path('/sys/bus/pci/devices/0000:b1:00.0')
r=pathlib.Path('/sys/class/drm/renderD135/device').resolve()
print(json.dumps({'pci':r.name,'numa':int((p/'numa_node').read_text()),
 'busy':int((p/'gpu_busy_percent').read_text()),
 'vram_bytes':int((p/'mem_info_vram_used').read_text()),
 'kfd_pids':sorted(x.name for x in pathlib.Path('/sys/class/kfd/kfd/proc').iterdir())}))
"""


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, default=str)
        stream.write("\n")


class BW20Transport:
    def __init__(self, runner: OpenSSHCommandRunner):
        self.runner = runner

    def checked(self, argv, timeout=60):
        result = self.runner.run(argv, timeout=timeout)
        if result.returncode:
            raise ExecutionSafetyError(
                f"remote {argv[0]} failed ({result.returncode}): "
                + result.stderr.decode(errors="replace")[:1500])
        return result.stdout

    def copy(self, source, destination, *, download=False):
        peer = f"{self.runner.user}@{self.runner.host}:"
        argv = ["scp", "-q", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-P", str(self.runner.port)]
        if self.runner.identity_file:
            argv.extend(["-i", str(self.runner.identity_file)])
        argv.extend([peer + str(source), str(destination)] if download
                    else [str(source), peer + str(destination)])
        subprocess.run(argv, check=True, timeout=120, capture_output=True)

    def state(self):
        return json.loads(self.checked(("python3", "-c", HOST_STATE)))

    def require_idle(self):
        state = self.state()
        if (state["pci"] != "0000:b1:00.0" or state["numa"] != 4
                or state["busy"] != 0 or state["vram_bytes"] > 4 * 1024 * 1024
                or state["kfd_pids"]):
            raise ExecutionSafetyError(f"HCU idle/identity check failed: {state}")
        return state

    def stage(self, directory, plan_hash):
        verify_prepared_pair(directory, expected_plan_sha256=plan_hash)
        plan = json.loads((directory / "plan.json").read_text())
        root = validate_run_root(plan["remote_run_root"])
        self.checked(("python3", "-c", """
import pathlib,sys
p=pathlib.Path(sys.argv[1])
if p.resolve(strict=True)!=p or not p.is_dir(): raise ValueError('redirected parent')
""", str(PurePosixPath(root).parent.parent)))
        self.checked(("mkdir", "-p", str(PurePosixPath(root).parent)))
        self.checked(("python3", "-c", """
import pathlib,sys
p=pathlib.Path(sys.argv[1])
if p.resolve(strict=True)!=p or not p.is_dir(): raise ValueError('redirected run parent')
""", str(PurePosixPath(root).parent)))
        self.checked(("mkdir", "--", root))  # Never reuse a run directory.
        for name in ("input", "baseline", "noop"):
            self.checked(("mkdir", "--", f"{root}/{name}"))
        for name in plan["input_sha256"]:
            self.copy(directory / "input" / name, f"{root}/input/{name}")
            self.checked(("chmod", "0444", f"{root}/input/{name}"))
        actual = json.loads(self.checked(("python3", "-c", INVENTORY, f"{root}/input")))
        if actual != plan["input_sha256"]:
            raise ExecutionSafetyError("remote input hashes differ from prepared plan")
        # Hash only the eight explicitly mounted model files, not the shared directory.
        model_mounts = plan["requests"][0]["mounts"][4:]
        for mount in model_mounts:
            name = mount["source"].rsplit("/", 1)[-1]
            output = self.checked(("sha256sum", "--", mount["source"])).decode().split()[0]
            if output != plan["expected_model_sha256"][name]:
                raise ExecutionSafetyError(f"remote model hash mismatch: {name}")
            self.checked(("python3", "-c", """
import pathlib,sys
p=pathlib.Path(sys.argv[1])
if p.resolve(strict=True)!=p or not p.is_file(): raise ValueError('redirected model file')
""", mount["source"]))
        self.checked(("python3", "-c", """
import os,pathlib,sys
r=pathlib.Path(sys.argv[1])
if str(r)!=str(r.resolve(strict=True)): raise ValueError('redirected output root')
for name in ('baseline','noop'):
 p=r/name
 if p.resolve()!=p or not p.is_dir() or any(p.iterdir()):
  raise ValueError('output is not a fresh directory')
 if p.stat().st_uid!=1002 or p.stat().st_gid!=1002:
  raise ValueError('output ownership mismatch')
 if not os.access(str(p),os.W_OK): raise ValueError('output is not writable')
""", root))
        # UID 1002 is not a passwd entry in the fixed image and HIP init fails.
        # Keep the proven non-root nobody user; grant only these fresh outputs.
        for name in ("baseline", "noop"):
            self.checked(("setfacl", "-m", "u:65534:rwx", f"{root}/{name}"))
        record = {"plan_sha256": plan_hash, "input_sha256": actual,
                  "model_hashes_verified": True, "host": self.require_idle(),
                  "container_user": "65534:65534", "output_acl_uid": 65534,
                  "production_profile_registered": False}
        write_json(directory / "remote-staging.json", record)

    def collect(self, root, variant, destination):
        validate_run_root(root)
        if variant not in {"baseline", "noop"}:
            raise ExecutionSafetyError("invalid variant")
        remote = f"{root}/{variant}"
        before = json.loads(self.checked(("python3", "-c", INVENTORY, remote)))
        if set(before) != VARIANT_FILES:
            raise ExecutionSafetyError(f"incomplete/unexpected {variant} evidence: {set(before)}")
        if any(destination.iterdir()):
            raise ExecutionSafetyError("local evidence directory is not empty")
        for name, digest in before.items():
            self.copy(f"{remote}/{name}", destination / name, download=True)
            if file_hash(destination / name) != digest:
                raise ExecutionSafetyError(f"download hash mismatch: {variant}/{name}")
        after = json.loads(self.checked(("python3", "-c", INVENTORY, remote)))
        if before != after:
            raise ExecutionSafetyError("remote evidence changed during collection")
        result = json.loads((destination / "result.json").read_text())
        response = json.loads((destination / "response.json").read_text())
        if result.get("status") == "succeeded":
            raw = normalize_response(response["body_json"]).model_dump(mode="json")
            if raw != result.get("normalized_output"):
                raise ExecutionSafetyError("raw response disagrees with normalized result")
        return before


class PreparedBW20Evaluator(SGLangSmokeEvaluator):
    def __init__(self, *, transport, snapshot, artifact_transport_path, run_id,
                 profile=PROFILE, **kwargs):
        super().__init__(profile=profile, **kwargs)
        self.transport = transport
        self.snapshot = snapshot
        self.artifact_transport_path = artifact_transport_path
        self.run_id = run_id
        self.prepared = None

    def prepare_framework_smoke(self, **kwargs):
        if kwargs["resource_id"] != RESOURCE_ID or kwargs["attempt_number"] != 1:
            raise ExecutionSafetyError("diagnostic pair requires first attempt on HCU7")
        self.prepared = prepare_pair(
            target=kwargs["target"], workload=load_workload_spec(self.workload_path),
            artifact=kwargs["artifact"], snapshot=self.snapshot,
            runner_path=self.runner_host_path,
            expected_runner_sha256=file_hash(self.runner_host_path),
            destination=kwargs["output_dir"] / "pair", fencing_token=kwargs["fencing_token"],
            run_id=self.run_id, artifact_transport_path=self.artifact_transport_path,
            request_ids=(kwargs["baseline_request_id"], kwargs["noop_request_id"]),
        )
        self.transport.stage(self.prepared.directory, self.prepared.plan_sha256)
        value = json.loads((self.prepared.directory / "plan.json").read_text())
        self.remote_root = value["remote_run_root"]
        requests = [ExecutionRequest.model_validate(r) for r in value["requests"]]
        return SGLangSmokePlan(self.prepared.directory, self.prepared.directory / "baseline",
                              self.prepared.directory / "noop", *requests)

    def evaluate_framework_smoke(self, plan, **kwargs):
        hashes = {}
        for variant in ("baseline", "noop"):
            hashes[variant] = self.transport.collect(
                self.remote_root, variant, getattr(plan, f"{variant}_evidence_dir"))
        write_json(plan.evidence_root / "transport-hashes.json", hashes)
        return super().evaluate_framework_smoke(plan, **kwargs)


class RepositoryLeaseGuard(FencingGuard):
    """Checks committed DB ownership AND expiry, not just a local token high-water."""
    def __init__(self, repository, job_id):
        super().__init__()
        self.repository, self.job_id = repository, job_id

    def check(self, resource_id, fencing_token):
        with self.repository.connection() as connection:
            row = connection.execute("""
                SELECT 1 FROM resources r JOIN jobs j ON j.job_id = r.owner_job_id
                WHERE r.resource_id=%s AND r.owner_job_id=%s AND r.fencing_token=%s
                  AND r.state='active' AND r.expires_at > clock_timestamp()
                  AND j.state='running' AND j.fencing_token=r.fencing_token
                  AND j.lease_id=r.lease_id AND j.resource_id=r.resource_id
                """, (resource_id, self.job_id, fencing_token)).fetchone()
        if row is None:
            raise StaleFencingToken("diagnostic job lease is absent, expired or superseded")

    def before_execute(self, resource_id, fencing_token):
        self.check(resource_id, fencing_token)
        super().before_execute(resource_id, fencing_token)

    def before_result(self, resource_id, fencing_token):
        self.check(resource_id, fencing_token)
        super().before_result(resource_id, fencing_token)


class BW20PairCleaner(ContainerResourceCleaner):
    """Limit original cleanup to this pair, including across independent test schemas."""
    def __init__(self, target, transport, request_ids, *, profile=PROFILE):
        super().__init__(target, transport.runner, profile=profile)
        self.provenance = self.provenance.model_copy(update={"adapter_name": type(self).__name__})
        self.transport, self.request_ids = transport, request_ids

    def _managed_container_ids(self, resource_id):
        if resource_id != RESOURCE_ID:
            raise ExecutionSafetyError("unexpected cleanup resource")
        found = []
        for request_id in self.request_ids:
            value = self.transport.checked((
                "docker", "ps", "-aq", "--filter", "label=io.hcuopt.managed=true",
                "--filter", f"label=io.hcuopt.resource-id={resource_id}",
                "--filter", f"label=io.hcuopt.request-id={request_id}",
            ))
            found.extend(value.decode().split())
        return found

    def health_check(self, resource_id):
        residual = self._managed_container_ids(resource_id)
        try:
            state = self.transport.require_idle()
            healthy = not residual
        except ExecutionSafetyError as exc:
            state, healthy = {"error": str(exc)}, False
        return {"healthy": healthy, "quarantined": not healthy, "host_state": state,
                "residual_owned_containers": residual, "clock_mutation_performed": False}


class DiagnosticRepositoryClient:
    """Worker transport for isolated-schema acceptance; explicitly NOT the HTTP API."""
    def __init__(self, repository, guard):
        self.repository, self.guard = repository, guard
        self.heartbeat_count = 0

    def register(self, worker_id, worker_type, capabilities, adapter_profile=None):
        self.repository.register_worker(WorkerRegister(
            worker_id=worker_id, worker_type=worker_type, capabilities=capabilities,
            adapter_profile=adapter_profile))

    def claim(self, worker_id):
        return self.repository.claim_job(worker_id)

    def heartbeat(self, worker_id, job):
        self.guard.check(job["resource_id"], job["fencing_token"])
        self.repository.heartbeat_job(worker_id, job["job_id"], job["claim_token"],
                                      job["fencing_token"])
        self.heartbeat_count += 1

    def complete(self, job, result):
        self.guard.check(job["resource_id"], job["fencing_token"])
        self.repository.complete_job(
            job["job_id"], job["claim_token"], job["fencing_token"], result)

    def fail(self, job, exc, cleanup_evidence=None):
        self.repository.fail_job(job["job_id"], job["claim_token"], job["fencing_token"],
                                 {"error_code": type(exc).__name__, "message": str(exc)},
                                 retryable=False, cleanup_evidence=cleanup_evidence)

    def report_cleanup(self, resource_id, fencing_token, cleanup_evidence):
        # No stale claim may release a resource through this diagnostic transport.
        raise ExecutionSafetyError("manual recovery needed after losing diagnostic lease")
