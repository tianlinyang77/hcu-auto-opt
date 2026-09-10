# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Transport only source/build jobs to the approved no-HCU CPU container scope."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from hcuopt.adapters.execution import OpenSSHCommandRunner
from hcuopt.contracts.v1 import NoopBuildResult, SourcePreparationResult
from hcuopt.deployment.bw20_build_worker import PROFILE, SHARED_SOURCE
from hcuopt.deployment.bw20_environment import IMAGE
from hcuopt.deployment.bw20_pair_bridge import BW20Transport, write_json
from hcuopt.deployment.kernel_binary_inventory import file_hash
from hcuopt.domain.errors import ExecutionSafetyError

REMOTE_PARENT = "/home/github/hcu-auto-opt-runtime/bw20-api-source"


def cpu_command(*, task_root: str, controller_root: str, request_path: str,
                container_name: str) -> tuple[str, ...]:
    root = PurePosixPath(task_root)
    if str(root.parent) != REMOTE_PARENT or str(UUID(root.name)) != root.name:
        raise ExecutionSafetyError("invalid CPU task root")
    controller = PurePosixPath(controller_root)
    prefix = "bw20-api-controller-"
    if (str(controller.parent) != "/home/github/hcu-auto-opt-runtime"
            or not controller.name.startswith(prefix)
            or str(UUID(controller.name[len(prefix):])) != controller.name[len(prefix):]):
        raise ExecutionSafetyError("invalid frozen CPU controller root")
    incoming = PurePosixPath(request_path)
    if incoming.parent != root / "requests" or incoming.suffix != ".json":
        raise ExecutionSafetyError("invalid job envelope path")
    UUID(incoming.stem)
    if container_name != "hcuopt-bw20-build-" + incoming.stem.replace("-", ""):
        raise ExecutionSafetyError("container name must match its request UUID")
    return (
        "docker", "run", "--rm", "--pull=never", "--init", "--name", container_name,
        "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--user=1002:1002",
        "--cpuset-cpus=64-65", "--cpuset-mems=4", "--memory=4g", "--memory-swap=4g",
        "--pids-limit=128", "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g",
        "--env", "HOME=/tmp", "--env", "PYTHONPATH=/workspace/src",
        "--env", "PYTHONNOUSERSITE=1",
        "--mount", f"type=bind,src={controller},dst=/workspace,readonly",
        "--mount", f"type=bind,src={SHARED_SOURCE},dst={SHARED_SOURCE},readonly",
        "--mount", f"type=bind,src={root},dst={root}",
        "--mount", f"type=bind,src={incoming},dst=/input/job.json,readonly",
        "--entrypoint", "/usr/bin/timeout", IMAGE,
        "--signal=TERM", "--kill-after=10s", "300s", "python3", "-m",
        "hcuopt.deployment.bw20_build_worker", "--request", "/input/job.json",
        "--task-root", str(root),
    )


class BW20BuildJobHandler:
    """Use with original Worker; no bypass of API claim/heartbeat/completion.

    The controller directory must have been staged and verified by deployment.
    Pin its manifest hash externally; validate it before each CPU invocation.
    """
    def __init__(self, *, runner: OpenSSHCommandRunner, controller_root: str,
                 controller_manifest_sha256: str, output_dir: Path):
        self.transport = BW20Transport(runner)
        self.controller_root = controller_root
        self.manifest_hash = controller_manifest_sha256
        self.output_dir = output_dir

    def handle(self, job_type, payload):
        if job_type not in {"source_prepare", "noop_build"}:
            raise ExecutionSafetyError("CPU build transport rejects non-build jobs")
        task_id = str(UUID(payload["task_id"]))
        job_id = str(UUID(str(payload["_job_context"]["job_id"])))
        request_id = str(uuid4())
        task_root = f"{REMOTE_PARENT}/{task_id}"
        request_path = f"{task_root}/requests/{request_id}.json"
        command = cpu_command(
            task_root=task_root, controller_root=self.controller_root, request_path=request_path,
            container_name="hcuopt-bw20-build-" + request_id.replace("-", ""))
        out = self.output_dir / "build-jobs" / job_id / request_id
        out.mkdir(parents=True, exist_ok=False)
        clean_payload = {k: v for k, v in payload.items() if k != "_job_context"}
        envelope = {"job_id": job_id, "job_type": job_type, "payload": clean_payload}
        write_json(out / "request.json", envelope)
        # Verify trusted controller content against a separately retained digest.
        self.transport.checked(("python3", "-c", """
import hashlib,json,pathlib,sys
r=pathlib.Path(sys.argv[1])
if r.resolve(strict=True)!=r: raise ValueError('redirected controller root')
p=r/'controller-manifest.json'
if hashlib.sha256(p.read_bytes()).hexdigest()!=sys.argv[2].split(':')[-1]:
 raise ValueError('controller manifest hash mismatch')
manifest=json.loads(p.read_text())
if {str(f.relative_to(r)) for f in r.rglob('*') if f.is_file()}!=set(manifest)|{p.name}:
 raise ValueError('controller inventory mismatch')
for name,expected in manifest.items():
 f=r/name
 if f.resolve(strict=True)!=f or r not in f.parents or not f.is_file():
  raise ValueError('redirected controller member')
 if hashlib.sha256(f.read_bytes()).hexdigest()!=expected:
  raise ValueError('controller member hash mismatch')
""",
            self.controller_root, self.manifest_hash))
        self.transport.checked(("mkdir", "-p", REMOTE_PARENT))
        self.transport.checked(("python3", "-c", """
import pathlib,sys
p=pathlib.Path(sys.argv[1])
if p.resolve(strict=True)!=p: raise ValueError('redirected source parent')
""", REMOTE_PARENT))
        if job_type == "source_prepare":
            self.transport.checked(("mkdir", "--", task_root))
            self.transport.checked(("mkdir", "--", f"{task_root}/requests"))
        self.transport.checked(("python3", "-c", """
import pathlib,sys
p=pathlib.Path(sys.argv[1])
if p.resolve(strict=True)!=p or not p.is_dir(): raise ValueError('redirected task root')
if (p/'requests').resolve(strict=True)!=p/'requests':
 raise ValueError('redirected request root')
""", task_root))
        self.transport.copy(out / "request.json", request_path)
        digest = self.transport.checked(("sha256sum", "--", request_path)).decode().split()[0]
        if "sha256:" + digest != file_hash(out / "request.json"):
            raise ExecutionSafetyError("uploaded job envelope hash differs")
        write_json(out / "command.json", {"argv": command, "profile": PROFILE, "hcu_used": False})
        executed = self.transport.runner.run(command, timeout=320)
        (out / "stdout.json").write_bytes(executed.stdout)
        (out / "stderr.log").write_bytes(executed.stderr)
        if executed.returncode:
            raise ExecutionSafetyError(f"CPU build job failed ({executed.returncode}); see {out}")
        record = json.loads(executed.stdout)
        if (record["job_id"] != job_id or record["task_id"] != task_id
                or record["job_type"] != job_type or record["profile"] != PROFILE
                or record["hcu_used"] or record["shared_before"] != record["shared_after"]):
            raise ExecutionSafetyError("CPU build response identity mismatch")
        response_type = SourcePreparationResult if job_type == "source_prepare" else NoopBuildResult
        result = response_type.model_validate(record["result"])
        if result.synthetic or any(p.implementation_kind != "real" or p.profile != PROFILE
                                   for p in result.adapter_provenance):
            raise ExecutionSafetyError("CPU build omitted real profile provenance")
        # A completed CPU call cannot remain as a managed container.
        residual = self.transport.checked((
            "docker", "ps", "-aq", "--filter", f"name=^/{command[command.index('--name')+1]}$"))
        if residual.strip():
            raise ExecutionSafetyError("CPU build container was not removed")
        return result.model_dump(mode="json")

    def cleanup(self, job_type, payload):
        # Build jobs have no HCU lease. Source cleanup remains in original Handler's finally.
        # Ambiguous transport failures are retained for explicit reconciliation, never replayed.
        return {}
