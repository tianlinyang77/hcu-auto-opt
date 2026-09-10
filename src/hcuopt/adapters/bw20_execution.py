# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Unregistered BW20 smoke policy; not a grant to run or a Formal evaluator.

The trusted staging layer must verify file hashes, real paths, fresh output ACLs,
PCI identity and a current resource window before invoking this adapter. This
module constrains argv/mount topology; it cannot attest remote file contents.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from uuid import UUID

from hcuopt.adapters.execution import CommandRunner, ContainerExecutionAdapter, FencingGuard
from hcuopt.contracts.platform_v1 import ExecutionRequest, MountSpec, TargetSpec
from hcuopt.deployment.bw20_environment import build_probe_plan
from hcuopt.deployment.bw20_smoke_preflight import MODEL, MODEL_HASHES
from hcuopt.domain.enums import LeaseScope
from hcuopt.domain.errors import ExecutionSafetyError

WORK_ROOT = "/home/github/hcu-auto-opt-runtime"
RUN_PARENT = f"{WORK_ROOT}/bw20-framework-smoke"
RESOURCE_ID = "bw20-sglang-0.5.12:hcu:7"
IMAGE_ID = "sha256:16fd28e795d55585657efe9a30ead1e2a457c8268e4fd21b53e8137fd9964012"
SMOKE_ARGV = (
    "/usr/bin/timeout", "--signal=TERM", "--kill-after=10s", "480s",
    "python3", "-I", "/opt/hcuopt/sglang_smoke_runner.py",
    "--spec", "/work/input/spec.json", "--evidence-dir", "/work/output",
)
# Copy on use: callers cannot expand the allowlist via a mutable class property.
_ENVIRONMENT = (
    ("HOME", "/tmp"), ("XDG_CACHE_HOME", "/tmp/cache"),
    ("HF_HOME", "/tmp/huggingface"), ("HF_HUB_OFFLINE", "1"),
    ("TRANSFORMERS_OFFLINE", "1"), ("TRITON_CACHE_DIR", "/tmp/triton"),
    ("OMP_NUM_THREADS", "8"), ("PYTHONNOUSERSITE", "1"),
    ("PYTHONPATH", ""), ("PYTHONHOME", ""), ("PYTHONOPTIMIZE", "0"),
    ("HIP_VISIBLE_DEVICES", "0"), ("ROCR_VISIBLE_DEVICES", "0"),
    ("HSA_VISIBLE_DEVICES", "0"),
)


def validate_run_root(value: str) -> str:
    path = PurePosixPath(value)
    try:
        identifier = UUID(path.name)
    except ValueError as exc:
        raise ExecutionSafetyError("BW20 run root must end in a canonical UUID") from exc
    if path.parent != PurePosixPath(RUN_PARENT) or value != f"{RUN_PARENT}/{identifier}":
        raise ExecutionSafetyError("BW20 run root must be a clean dedicated run directory")
    return value


def smoke_mounts(run_root: str, variant: str) -> list[MountSpec]:
    """Expected staging layout only; does not create files or verify an artifact."""
    validate_run_root(run_root)
    if variant not in {"baseline", "noop"}:
        raise ExecutionSafetyError("only baseline/noop smoke variants are supported")
    mounts = [
        MountSpec(source="/opt/hyhal", target="/opt/hyhal", read_only=True),
        MountSpec(source=f"{run_root}/input/sglang_smoke_runner.py",
                  target="/opt/hcuopt/sglang_smoke_runner.py", read_only=True),
        MountSpec(source=f"{run_root}/input/spec.json",
                  target="/work/input/spec.json", read_only=True),
        MountSpec(source=f"{run_root}/{variant}", target="/work/output", read_only=False),
    ]
    mounts.extend(MountSpec(source=f"{MODEL}/{name}", target=f"{MODEL}/{name}", read_only=True)
                  for name in MODEL_HASHES)
    if variant == "noop":
        mounts.append(MountSpec(source=f"{run_root}/input/noop-source.tar",
                                target="/opt/hcuopt/artifacts/noop-source.tar", read_only=True))
    return mounts


class BW20SmokeExecutionAdapter(ContainerExecutionAdapter):
    """Explicit opt-in policy sharing the original timeout/cancel/fencing lifecycle.

    This is deliberately NOT registered in an Adapter Profile. A no-op tar is
    mounted only, not imported, installed, or executed as candidate code.
    """

    def __init__(self, runner: CommandRunner, *, run_root: str,
                 fencing_guard: FencingGuard | None = None,
                 profile: str = "bw20-smoke-policy-unregistered-v1",
                 poll_interval_seconds: float = 0.1) -> None:
        if profile not in {"bw20-smoke-policy-unregistered-v1", "bw20-framework-smoke-v1"}:
            raise ExecutionSafetyError("unsupported BW20 deployment profile")
        self.run_root = validate_run_root(run_root)
        super().__init__(runner, profile=profile,
                         adapter_name=type(self).__name__, fencing_guard=fencing_guard,
                         poll_interval_seconds=poll_interval_seconds)

    def validate_scope(self, request: ExecutionRequest, target: TargetSpec) -> None:
        """Pure request validation; no runner calls, lease grant or remote attestation."""
        try:
            build_probe_plan(target)
        except ValueError as exc:
            raise ExecutionSafetyError(str(exc)) from exc
        if (target.inference_image.image_id != IMAGE_ID
                or target.execution_host.work_root != WORK_ROOT):
            raise ExecutionSafetyError("BW20 locked image/work root drift")
        super()._validate_request(request, target)
        if request.lease_scope is not LeaseScope.EXCLUSIVE or request.resource_id != RESOURCE_ID:
            raise ExecutionSafetyError("BW20 smoke requires its exclusive resource lease")
        if (tuple(request.argv) != SMOKE_ARGV or request.working_directory != "/work"
                or not 1 <= request.timeout_seconds <= 480):
            raise ExecutionSafetyError("BW20 smoke argv/workdir/timeout exceeds fixed scope")
        def canonical(mounts: list[MountSpec]) -> list[tuple[str, str, bool]]:
            return sorted((m.source, m.target, m.read_only) for m in mounts)

        actual = canonical(request.mounts)
        if not any(actual == canonical(smoke_mounts(self.run_root, variant))
                   for variant in ("baseline", "noop")):
            raise ExecutionSafetyError("BW20 smoke mounts differ from exact staging layout")

    def _validate_request(self, request: ExecutionRequest, target: TargetSpec) -> None:
        self.validate_scope(request, target)

    @staticmethod
    def _container_environment(request: ExecutionRequest, target: TargetSpec) -> dict[str, str]:
        expected = dict(_ENVIRONMENT)
        if any(key not in expected or expected[key] != value
               for key, value in request.environment.items()):
            raise ExecutionSafetyError("BW20 environment must match fixed logical-device-0 policy")
        return expected

    def _resource_arguments(self, request: ExecutionRequest, target: TargetSpec) -> tuple[str, ...]:
        return (
            "--network=none", "--read-only", "--cap-drop=ALL",
            "--user=65534:65534",
            "--cpuset-cpus=64-79", "--cpuset-mems=4",
            "--memory=16g", "--memory-swap=16g", "--pids-limit=512",
            "--device=/dev/kfd", "--device=/dev/dri/renderD135",
            "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=4g", "--shm-size=2g",
            "--ulimit", "fsize=2147483648:2147483648",
        )

    def _execution_metadata(
        self, request: ExecutionRequest, target: TargetSpec
    ) -> dict[str, object]:
        return {
            "execution_policy": "bw20-single-render-smoke-v1",
            "logical_device_index": 0, "render_node": "/dev/dri/renderD135",
            "expected_pci_address": "0000:b1:00.0",
            "runtime_pci_verified_by_policy": False,
            "production_profile_registered": False,
            "performance_conclusion": "not_measured", "automatic_release_allowed": False,
        }
