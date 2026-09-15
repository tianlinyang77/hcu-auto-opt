# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Fail-closed BW20 policy shared by the M1 correctness/performance adapters.

This module is configuration and validation only.  Importing it does not register
the profile, acquire HCU 7, start Docker, mutate clocks, or authorize a Candidate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hcuopt.adapters.bw20_execution import IMAGE_ID, RESOURCE_ID
from hcuopt.adapters.profiles import BW20_MANUAL_CANDIDATE_PROFILE
from hcuopt.contracts.platform_v1 import TargetSpec
from hcuopt.domain.enums import LeaseScope
from hcuopt.domain.errors import ExecutionSafetyError


@dataclass(frozen=True, slots=True)
class BW20M1Policy:
    profile: str = BW20_MANUAL_CANDIDATE_PROFILE
    target_id: str = "bw20-sglang-0.5.12"
    host_name: str = "github-bw20"
    host_address: str = "10.17.1.20"
    resource_id: str = RESOURCE_ID
    physical_device_index: int = 7
    logical_device_index: int = 0
    render_node: str = "/dev/dri/renderD135"
    pci_address: str = "0000:b1:00.0"
    architecture: str = "gfx936"
    numa_node: int = 4
    cpu_affinity: str = "64-79"
    performance_level: str = "auto"
    image_id: str = IMAGE_ID

    def validate_target(self, target: TargetSpec) -> None:
        host = target.execution_host
        accelerator = host.accelerator
        if (
            target.target_id != self.target_id
            or (host.name, host.address) != (self.host_name, self.host_address)
            or target.inference_image.image_id != self.image_id
            or (
                accelerator.device_index,
                accelerator.numa_node,
                accelerator.cpu_affinity,
                accelerator.architecture,
                accelerator.expected_performance_level,
            )
            != (
                self.physical_device_index,
                self.numa_node,
                self.cpu_affinity,
                self.architecture,
                self.performance_level,
            )
        ):
            raise ExecutionSafetyError("BW20 M1 Target binding differs from the Target Lock")

    def require_job_context(
        self,
        payload: Mapping[str, Any],
        expected_scope: LeaseScope,
    ) -> dict[str, Any]:
        raw = payload.get("_job_context")
        if not isinstance(raw, Mapping):
            raise ExecutionSafetyError("BW20 M1 execution requires durable Job context")
        context = dict(raw)
        token = context.get("fencing_token")
        if (
            context.get("lease_scope") != expected_scope.value
            or context.get("resource_id") != self.resource_id
            or isinstance(token, bool)
            or not isinstance(token, int)
            or token < 1
            or not context.get("lease_id")
        ):
            raise ExecutionSafetyError("BW20 M1 lease binding is incomplete or cross-target")
        return context

    def docker_resource_arguments(self) -> tuple[str, ...]:
        """Exact single-render sandbox; output/cache mounts are added by the caller."""

        return (
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=65534:65534",
            "--cpuset-cpus=64-79",
            "--cpuset-mems=4",
            "--memory=16g",
            "--memory-swap=16g",
            "--pids-limit=512",
            "--device=/dev/kfd",
            "--device=/dev/dri/renderD135",
            "--tmpfs=/tmp:rw,exec,nosuid,nodev,size=4g",
            "--shm-size=2g",
            "--ulimit=fsize=2147483648:2147483648",
        )

    def container_environment(self) -> tuple[str, ...]:
        return (
            "HOME=/tmp",
            "XDG_CACHE_HOME=/tmp/cache",
            "HF_HOME=/tmp/huggingface",
            "HF_HUB_OFFLINE=1",
            "TRANSFORMERS_OFFLINE=1",
            "TRITON_CACHE_DIR=/tmp/triton",
            "OMP_NUM_THREADS=8",
            "PYTHONNOUSERSITE=1",
            "PYTHONDONTWRITEBYTECODE=1",
            "HIP_VISIBLE_DEVICES=0",
            "ROCR_VISIBLE_DEVICES=0",
            "HSA_VISIBLE_DEVICES=0",
            "PYTHONPATH=/workspace/src",
        )


BW20_M1_POLICY = BW20M1Policy()


__all__ = ["BW20M1Policy", "BW20_M1_POLICY"]
