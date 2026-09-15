# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Job-owned C executor; reuse the original execution result/lifecycle contract.

Run the C controller on BW20 with a shared host filesystem view. This does not
grant Docker access, prepare sources, register profiles or attest performance.
"""

from __future__ import annotations

import threading
from pathlib import Path

from hcuopt.adapters.bw20_execution import IMAGE_ID
from hcuopt.adapters.execution import ContainerExecutionAdapter, FencingGuard
from hcuopt.deployment.bw20_environment import build_probe_plan
from hcuopt.deployment.bw20_stage0_harness import PROFILE
from hcuopt.deployment.bw20_stage0_runtime import RESOURCE, BW20TimingPlan
from hcuopt.deployment.bw20_timing_transport import BW20DockerTransport
from hcuopt.domain.enums import LeaseScope
from hcuopt.domain.errors import ExecutionSafetyError
from hcuopt.runtime_probes.evidence import sha256_file
from hcuopt.runtime_probes.overlay import OverlayCapabilityProbe
from hcuopt.runtime_probes.profile import RuntimeProbeProfile
from hcuopt.targets import target_fingerprint


class _LiveFence(FencingGuard):
    def __init__(self, context, stopped):
        super().__init__()
        self.context, self.stopped = dict(context), stopped

    def check(self):
        if self.stopped.is_set() or self.context["assert_live_lease"]() is not None:
            raise ExecutionSafetyError("C job lost authority or stopped")

    def before_execute(self, resource_id, fencing_token):
        if resource_id != RESOURCE or fencing_token != self.context["fencing_token"]:
            raise ExecutionSafetyError("C execution lease mismatch")
        self.check()
        super().before_execute(resource_id, fencing_token)

    def before_result(self, resource_id, fencing_token):
        self.before_execute(resource_id, fencing_token)


class _LeaseRunner:
    def __init__(self, runner, owner):
        self.runner, self.owner = runner, owner

    def run(self, *args, **kwargs):
        return self.runner.run(*args, **kwargs)

    def popen(self, *args, **kwargs):
        self.owner.fencing_guard.check()
        process = self.runner.popen(*args, **kwargs)
        owner = self.owner

        class Process:
            def poll(self):
                try:
                    owner.fencing_guard.check()
                except Exception:
                    owner.force_close()
                    process.kill()
                    process.wait(timeout=5)
                    raise
                return process.poll()

            def wait(self, timeout=None):
                return process.wait(timeout=timeout)

            def kill(self):
                return process.kill()

        return Process()


class BW20CapabilityExecutionAdapter(ContainerExecutionAdapter):
    def __init__(
        self, *, runner, target, configuration: RuntimeProbeProfile, context, evidence_root
    ):
        from hcuopt.deployment.bw20_stage0_adapter import BW20Stage0ProbeAdapter

        BW20Stage0ProbeAdapter._key({"_job_context": context})
        if not callable(context.get("assert_live_lease")):
            raise ValueError("original live lease callback required")
        build_probe_plan(target)
        self.configuration = RuntimeProbeProfile.model_validate_json(
            configuration.model_dump_json()
        )
        if (
            configuration.target_fingerprint != target_fingerprint(target)
            or configuration.profile != PROFILE
        ):
            raise ValueError("C configuration does not bind this deployment")
        self.target, self.context = target.model_copy(deep=True), dict(context)
        self.evidence_root = Path(evidence_root).resolve(strict=True)
        self.transport = BW20DockerTransport(runner=runner, target=target)
        self.stopped = threading.Event()
        self.creation_uncertain = False
        self._cids = {}
        super().__init__(
            runner,
            profile=PROFILE,
            adapter_name=type(self).__name__,
            fencing_guard=_LiveFence(context, self.stopped),
            poll_interval_seconds=0.25,
        )
        self.runner = _LeaseRunner(runner, self)

    def _validate_request(self, request, target):
        super()._validate_request(request, target)
        if (
            target_fingerprint(target) != self.configuration.target_fingerprint
            or target.inference_image.image_id != IMAGE_ID
            or request.lease_scope is not LeaseScope.EXCLUSIVE
            or request.resource_id != RESOURCE
            or request.fencing_token != self.context["fencing_token"]
        ):
            raise ExecutionSafetyError("C target/resource mismatch")
        actual = request.model_dump(exclude={"request_id", "timeout_seconds"})
        candidates = []
        profiler = self.configuration.profiler
        for tool in profiler.tool_candidates:
            for argv in (tool.version_argv, tool.profile_argv):
                candidates.append(
                    (
                        dict(
                            target_id=target.target_id,
                            argv=list(argv),
                            working_directory=profiler.working_directory,
                            environment=profiler.environment,
                            lease_scope=LeaseScope.EXCLUSIVE,
                            resource_id=RESOURCE,
                            fencing_token=request.fencing_token,
                            container_image=request.container_image,
                            mounts=[m.model_dump() for m in profiler.mounts],
                        ),
                        min(tool.timeout_seconds, 600),
                    )
                )
        overlay = self.configuration.hotpatch
        for name in ("baseline", "candidate", "recovery"):
            phase = getattr(overlay, name)
            expected = OverlayCapabilityProbe._execution_request(
                phase,
                target,
                RESOURCE,
                request.fencing_token,
                artifact=overlay.artifact if name == "candidate" else None,
                overlay_target=overlay.overlay_mount_target if name == "candidate" else None,
                lease_scope=LeaseScope.EXCLUSIVE,
            ).model_dump(exclude={"request_id", "timeout_seconds"})
            if name == "candidate":
                # Original C republishes the immutable artifact under its trusted
                # evidence root. Only that content-equivalent mount may change.
                matching = [m for m in request.mounts if m.target == overlay.overlay_mount_target]
                if len(matching) == 1 and matching[0].read_only:
                    path = Path(matching[0].source)
                    if (
                        path.is_file()
                        and not path.is_symlink()
                        and path.resolve() == path
                        and self.evidence_root in path.parents
                        and sha256_file(path) == overlay.artifact.content_hash
                    ):
                        for mount in expected["mounts"]:
                            if mount["target"] == overlay.overlay_mount_target:
                                mount["source"] = matching[0].source
            candidates.append((expected, min(phase.timeout_seconds, 600)))
        if not any(
            actual == expected and request.timeout_seconds <= limit
            for expected, limit in candidates
        ):
            raise ExecutionSafetyError("C request exceeds frozen configuration")
        if self.stopped.is_set():
            raise ExecutionSafetyError("C job already stopped")

    @staticmethod
    def _container_environment(request, target):
        # Validate general names/secrets without inheriting physical device 7.
        raw = request.model_copy(update={"lease_scope": LeaseScope.NONE})
        env = ContainerExecutionAdapter._container_environment(raw, target)
        for key in ("HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "HSA_VISIBLE_DEVICES"):
            if key in env and env[key] != "0":
                raise ExecutionSafetyError("C requires logical device zero")
            env[key] = "0"
        env.setdefault("HOME", "/tmp")
        return env

    def _resource_arguments(self, request, target):
        return (
            "--network=none",
            "--user=65534:65534",
            "--ipc=private",
            "--read-only",
            "--cap-drop=ALL",
            "--cpuset-cpus=64-79",
            "--cpuset-mems=4",
            "--memory=16g",
            "--memory-swap=16g",
            "--pids-limit=512",
            "--device=/dev/kfd",
            "--device=/dev/dri/renderD135",
            "--tmpfs",
            "/tmp:rw,exec,nosuid,nodev,size=4g",
            "--shm-size=2g",
        )

    def _docker_command(self, request, target, *, container_name, resource_id, fencing_token):
        writable = [Path(mount.source) for mount in request.mounts if not mount.read_only]
        if len(writable) > 1:
            raise ExecutionSafetyError("C execution supports one writable evidence mount")
        for path in writable:
            if (
                path.resolve(strict=True) != path
                or not path.is_dir()
                or self.evidence_root not in path.parents
            ):
                raise ExecutionSafetyError("C writable evidence mount is outside its job root")
            result = self.runner.run(
                ("setfacl", "-m", "u:65534:rwx", "--", str(path)), timeout=15
            )
            if result.returncode:
                raise ExecutionSafetyError("C could not grant the container evidence ACL")
        argv = super()._docker_command(
            request,
            target,
            container_name=container_name,
            resource_id=resource_id,
            fencing_token=fencing_token,
        )
        plan = BW20TimingPlan(argv, container_name, resource_id, fencing_token, "")
        self.fencing_guard.check()
        self.creation_uncertain = True
        cid = self.transport._create(plan, 15)
        self._cids[container_name] = cid
        self.creation_uncertain = False
        try:
            raw = self.transport.inspect(cid, 15)
            self.transport._ownership(raw, cid)
            host = raw["HostConfig"]
            env = dict(entry.split("=", 1) for entry in raw["Config"]["Env"])
            expected_mounts = sorted((m.source, m.target, not m.read_only) for m in request.mounts)
            if (
                raw["Image"] != IMAGE_ID
                or raw["Config"].get("User") != "65534:65534"
                or raw["Config"]["Image"] != target.inference_image.immutable_reference
                or raw["Config"]["WorkingDir"] != request.working_directory
                or any(
                    env.get(k) != v for k, v in self._container_environment(request, target).items()
                )
                or raw["State"]["Running"] is not False
                or host["NetworkMode"] != "none"
                or host["Privileged"]
                or host["PidMode"] not in ("", "private")
                or host["IpcMode"] != "private"
                or host["SecurityOpt"] not in (["no-new-privileges"], ["no-new-privileges:true"])
                or host["AutoRemove"] is not True
                or host["RestartPolicy"]["Name"] != "no"
                or host["Tmpfs"] != {"/tmp": "rw,exec,nosuid,nodev,size=4g"}
                or not host["ReadonlyRootfs"]
                or host["CapDrop"] != ["ALL"]
                or host.get("CapAdd")
                or host.get("DeviceRequests")
                or host.get("DeviceCgroupRules")
                or host["CpusetCpus"] != "64-79"
                or host["CpusetMems"] != "4"
                or host["Memory"] != 16 * 1024**3
                or host["MemorySwap"] != 16 * 1024**3
                or host["PidsLimit"] != 512
                or host["ShmSize"] != 2 * 1024**3
                or sorted(
                    (d["PathOnHost"], d["PathInContainer"], d["CgroupPermissions"])
                    for d in host["Devices"]
                )
                != [
                    ("/dev/dri/renderD135", "/dev/dri/renderD135", "rwm"),
                    ("/dev/kfd", "/dev/kfd", "rwm"),
                ]
                or sorted((m["Source"], m["Destination"], m["RW"]) for m in raw["Mounts"])
                != expected_mounts
                or any(m["Type"] != "bind" for m in raw["Mounts"])
                or raw["Config"]["Entrypoint"] != request.argv[:1]
                or raw["Config"]["Cmd"] != request.argv[1:]
            ):
                raise ExecutionSafetyError("C created container differs from bounded policy")
            self.fencing_guard.check()
            return ("docker", "start", "--attach", cid)
        except BaseException:
            self.force_close()
            raise

    def _terminate_owned_container(self, owned, reason):
        cid = self._cids.get(owned.container_name)
        removed = False
        if cid:
            self.transport.remove(cid, 15)
            removed = self.transport.inspect(cid, 15) is None
        return dict(requested=True, reason=reason, container_removed=removed)

    def force_close(self):
        self.stopped.set()
        healthy = not self.creation_uncertain
        for cid in tuple(self.transport.owned):
            try:
                self.transport.remove(cid, 15)
                healthy = (self.transport.inspect(cid, 15) is None) and healthy
            except Exception:
                healthy = False
        return healthy

    def confirm_empty(self):
        if self.creation_uncertain:
            return False
        try:
            return all(
                self.transport.inspect(cid, 15) is None for cid in tuple(self.transport.owned)
            )
        except Exception:
            return False

    def _execution_metadata(self, request, target):
        return dict(
            execution_policy="bw20-stage0-capability-v1",
            logical_device_index=0,
            container_name=self._cids.get(f"hcuopt-{request.request_id.hex}"),
            performance_conclusion="not_measured",
            automatic_release_allowed=False,
        )
