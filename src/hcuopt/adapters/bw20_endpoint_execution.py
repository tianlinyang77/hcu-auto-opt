# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Narrow BW20 policy for one provisional SGLang endpoint acquisition."""

from __future__ import annotations

from pathlib import PurePosixPath
from uuid import UUID

from hcuopt.adapters.bw20_execution import IMAGE_ID, RESOURCE_ID, WORK_ROOT
from hcuopt.adapters.execution import CommandRunner, ContainerExecutionAdapter, FencingGuard
from hcuopt.contracts.platform_v1 import ExecutionRequest, MountSpec, TargetSpec
from hcuopt.deployment.bw20_environment import build_probe_plan
from hcuopt.deployment.bw20_smoke_preflight import MODEL, MODEL_HASHES
from hcuopt.domain.enums import LeaseScope
from hcuopt.domain.errors import ExecutionSafetyError

PROFILE = "bw20-endpoint-provisional-v1"
RUN_PARENT = f"{WORK_ROOT}/bw20-endpoint-validation"
TARGET_MODULE_NAME = "sglang.srt.mem_cache.allocator"
TARGET_MODULE_PATH = "/usr/local/lib/python3.10/dist-packages/sglang/srt/mem_cache/allocator.py"
BASELINE_MODULE_HASH = "sha256:ef09cd90dd03a542e586c70b4baa805b9d9ab24f84e4e8c20b6fc313f6f5fe27"
CANDIDATE_MODULE_HASH = "sha256:93bd2a5aec5a01cb61ac8c6f2ddca7bd25d63ffe4aec8fed7f60199c3581da8a"
SIGNED_ARTIFACT_PATH = (
    f"{WORK_ROOT}/bw20-agent-m1-20260915-v2/formal-build-v10/artifacts/sha256/93/"
    "bd2a5aec5a01cb61ac8c6f2ddca7bd25d63ffe4aec8fed7f60199c3581da8a"
)
ENDPOINT_ARGV = (
    "/usr/bin/timeout",
    "--signal=TERM",
    "--kill-after=15s",
    "600s",
    "python3",
    "/opt/hcuopt/src/hcuopt/evaluation/sglang_endpoint_runner.py",
    "--spec",
    "/work/input/spec.json",
    "--evidence-dir",
    "/work/output",
)


def validate_endpoint_run_root(value: str) -> str:
    path = PurePosixPath(value)
    try:
        identifier = UUID(path.name)
    except ValueError as exc:
        raise ExecutionSafetyError("BW20 endpoint root must end in a canonical UUID") from exc
    if path.parent != PurePosixPath(RUN_PARENT) or value != f"{RUN_PARENT}/{identifier}":
        raise ExecutionSafetyError("BW20 endpoint root must be a dedicated run directory")
    return value


def endpoint_mounts(
    run_root: str,
    arm: str,
    acquisition_ordinal: int,
) -> list[MountSpec]:
    validate_endpoint_run_root(run_root)
    if arm not in {"baseline", "candidate"}:
        raise ExecutionSafetyError("endpoint arm must be baseline or candidate")
    if not 0 <= acquisition_ordinal <= 1_000_000:
        raise ExecutionSafetyError("endpoint acquisition ordinal is outside the allowed range")
    name = f"{acquisition_ordinal:04d}-{arm}"
    mounts = [
        MountSpec(source="/opt/hyhal", target="/opt/hyhal", read_only=True),
        MountSpec(
            source=f"{run_root}/input/src",
            target="/opt/hcuopt/src",
            read_only=True,
        ),
        MountSpec(
            source=f"{run_root}/input/activation",
            target="/opt/hcuopt/activation",
            read_only=True,
        ),
        MountSpec(
            source=f"{run_root}/input/{name}-spec.json",
            target="/work/input/spec.json",
            read_only=True,
        ),
        MountSpec(
            source=f"{run_root}/acquisitions/{name}",
            target="/work/output",
            read_only=False,
        ),
    ]
    mounts.extend(
        MountSpec(
            source=f"{run_root}/model/{filename}",
            target=f"{MODEL}/{filename}",
            read_only=True,
        )
        for filename in MODEL_HASHES
    )
    if arm == "candidate":
        mounts.append(
            MountSpec(
                source=SIGNED_ARTIFACT_PATH,
                target=TARGET_MODULE_PATH,
                read_only=True,
            )
        )
    return mounts


class BW20EndpointExecutionAdapter(ContainerExecutionAdapter):
    """Exact container boundary; staging and live lease still remain external guards."""

    def __init__(
        self,
        runner: CommandRunner,
        *,
        run_root: str,
        fencing_guard: FencingGuard | None = None,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        self.run_root = validate_endpoint_run_root(run_root)
        super().__init__(
            runner,
            profile=PROFILE,
            adapter_name=type(self).__name__,
            fencing_guard=fencing_guard,
            poll_interval_seconds=poll_interval_seconds,
        )

    def validate_scope(self, request: ExecutionRequest, target: TargetSpec) -> None:
        try:
            build_probe_plan(target)
        except ValueError as exc:
            raise ExecutionSafetyError(str(exc)) from exc
        if (
            target.inference_image.image_id != IMAGE_ID
            or target.execution_host.work_root != WORK_ROOT
        ):
            raise ExecutionSafetyError("BW20 endpoint Target Lock differs")
        super()._validate_request(request, target)
        if request.lease_scope is not LeaseScope.EXCLUSIVE or request.resource_id != RESOURCE_ID:
            raise ExecutionSafetyError("BW20 endpoint acquisition requires the HCU 7 lease")
        if (
            tuple(request.argv) != ENDPOINT_ARGV
            or request.working_directory != "/work"
            or not 1 <= request.timeout_seconds <= 600
        ):
            raise ExecutionSafetyError("BW20 endpoint argv/workdir/timeout differs")
        matches: list[tuple[str, int]] = []
        actual = _canonical_mounts(request.mounts)
        for ordinal in range(4):
            for arm in ("baseline", "candidate"):
                if actual == _canonical_mounts(endpoint_mounts(self.run_root, arm, ordinal)):
                    matches.append((arm, ordinal))
        if len(matches) != 1:
            raise ExecutionSafetyError("BW20 endpoint mounts differ from the exact layout")

    def _validate_request(self, request: ExecutionRequest, target: TargetSpec) -> None:
        self.validate_scope(request, target)

    @staticmethod
    def _container_environment(request: ExecutionRequest, target: TargetSpec) -> dict[str, str]:
        candidate = any(mount.target == TARGET_MODULE_PATH for mount in request.mounts)
        expected = {
            "HOME": "/tmp",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "OMP_NUM_THREADS": "8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": "/opt/hcuopt/activation:/opt/hcuopt/src",
            "HIP_VISIBLE_DEVICES": "0",
            "ROCR_VISIBLE_DEVICES": "0",
            "HSA_VISIBLE_DEVICES": "0",
            "HCUOPT_ENDPOINT_TARGET_MODULE": TARGET_MODULE_NAME,
            "HCUOPT_ENDPOINT_TARGET_PATH": TARGET_MODULE_PATH,
            "HCUOPT_ENDPOINT_TARGET_SHA256": (
                CANDIDATE_MODULE_HASH if candidate else BASELINE_MODULE_HASH
            ),
            "HCUOPT_ENDPOINT_ACTIVATION_PATH": "/work/output/activation.json",
        }
        if any(
            key not in expected or expected[key] != value
            for key, value in request.environment.items()
        ):
            raise ExecutionSafetyError("BW20 endpoint environment differs from policy")
        return expected

    def _resource_arguments(self, request: ExecutionRequest, target: TargetSpec) -> tuple[str, ...]:
        return (
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--user=65534:65534",
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
            "--ulimit",
            "fsize=2147483648:2147483648",
        )

    def _execution_metadata(
        self, request: ExecutionRequest, target: TargetSpec
    ) -> dict[str, object]:
        candidate = any(mount.target == TARGET_MODULE_PATH for mount in request.mounts)
        return {
            "execution_policy": PROFILE,
            "arm": "candidate" if candidate else "baseline",
            "logical_device_index": 0,
            "render_node": "/dev/dri/renderD135",
            "expected_pci_address": "0000:b1:00.0",
            "runtime_pci_verified_by_policy": False,
            "run_mode": "provisional",
            "performance_conclusion": "not_adjudicated",
            "automatic_release_allowed": False,
        }


def _canonical_mounts(mounts: list[MountSpec]) -> list[tuple[str, str, bool]]:
    return sorted((item.source, item.target, item.read_only) for item in mounts)


__all__ = [
    "BASELINE_MODULE_HASH",
    "BW20EndpointExecutionAdapter",
    "CANDIDATE_MODULE_HASH",
    "ENDPOINT_ARGV",
    "PROFILE",
    "RUN_PARENT",
    "SIGNED_ARTIFACT_PATH",
    "TARGET_MODULE_PATH",
    "endpoint_mounts",
    "validate_endpoint_run_root",
]
