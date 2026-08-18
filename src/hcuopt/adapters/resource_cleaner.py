from __future__ import annotations

import json
from collections.abc import Mapping

from hcuopt.adapters.execution import (
    FENCING_LABEL,
    MANAGED_LABEL,
    RESOURCE_LABEL,
    CommandRunner,
    LocalCommandRunner,
)
from hcuopt.contracts.platform_v1 import AdapterProvenance, TargetSpec


class ContainerResourceCleaner:
    """Fence only adapter-owned containers, then verify the locked device is healthy."""

    def __init__(
        self,
        target: TargetSpec,
        runner: CommandRunner | None = None,
        *,
        profile: str = "nmz36-framework-smoke-v1",
    ) -> None:
        self.target = target
        self.runner = runner or LocalCommandRunner()
        self.provenance = AdapterProvenance(
            profile=profile,
            capability="resource_cleaner",
            adapter_name="ContainerResourceCleaner",
            adapter_version="1.0.0",
            implementation_kind="real",
        )

    def fence(self, resource_id: str, fencing_token: int) -> Mapping[str, object]:
        removed: list[str] = []
        failures: list[dict[str, object]] = []
        skipped_newer: list[str] = []
        for container_id in self._managed_container_ids(resource_id):
            labels = self._labels(container_id)
            raw_token = labels.get(FENCING_LABEL)
            try:
                token = int(raw_token)
            except (TypeError, ValueError):
                failures.append(
                    {
                        "container_id": container_id,
                        "reason": "missing_or_invalid_fencing_label",
                    }
                )
                continue
            if token > fencing_token:
                skipped_newer.append(container_id)
                continue
            result = self.runner.run(("docker", "rm", "--force", container_id), timeout=30)
            if result.returncode == 0 or b"No such container" in result.stderr:
                removed.append(container_id)
            else:
                failures.append(
                    {
                        "container_id": container_id,
                        "reason": "docker_rm_failed",
                        "exit_code": result.returncode,
                        "stderr": _bounded_text(result.stderr),
                    }
                )
        return {
            "resource_id": resource_id,
            "fencing_token": fencing_token,
            "owned_containers_removed": removed,
            "newer_containers_preserved": skipped_newer,
            "failures": failures,
            "fenced": not failures,
            "clock_mutation_performed": False,
            "clocks_restored": True,
        }

    def health_check(self, resource_id: str) -> Mapping[str, object]:
        residual = self._managed_container_ids(resource_id)
        device = self.target.execution_host.accelerator.device_index
        health = self.runner.run(
            (
                "hy-smi",
                "-d",
                str(device),
                "--showuse",
                "--showmemuse",
                "--showperflevel",
                "--showclocks",
            ),
            timeout=30,
        )
        healthy = health.returncode == 0 and not residual
        return {
            "resource_id": resource_id,
            "device_index": device,
            "healthy": healthy,
            "quarantined": not healthy,
            "residual_owned_containers": residual,
            "device_check_exit_code": health.returncode,
            "device_check_stdout": _bounded_text(health.stdout),
            "device_check_stderr": _bounded_text(health.stderr),
            "memory_release_check": "no_adapter_owned_container_remains",
            "clock_mutation_performed": False,
            "clocks_restored": True,
        }

    def cleanup(self, resource_id: str, fencing_token: int) -> dict[str, Mapping[str, object]]:
        fence = self.fence(resource_id, fencing_token)
        health = self.health_check(resource_id)
        return {"fence": fence, "health": health}

    def _managed_container_ids(self, resource_id: str) -> list[str]:
        result = self.runner.run(
            (
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label={MANAGED_LABEL}=true",
                "--filter",
                f"label={RESOURCE_LABEL}={resource_id}",
            ),
            timeout=30,
        )
        if result.returncode != 0:
            return ["<docker-list-failed>"]
        return [line for line in result.stdout.decode().splitlines() if line]

    def _labels(self, container_id: str) -> dict[str, str]:
        result = self.runner.run(
            (
                "docker",
                "inspect",
                "--format",
                "{{json .Config.Labels}}",
                container_id,
            ),
            timeout=30,
        )
        if result.returncode != 0:
            return {}
        try:
            labels = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {}
        if not isinstance(labels, dict):
            return {}
        return {str(key): str(value) for key, value in labels.items()}


def cleanup_is_healthy(evidence: Mapping[str, object] | None) -> bool:
    if not evidence:
        return False
    fence = evidence.get("fence")
    health = evidence.get("health")
    if not isinstance(fence, Mapping) or not isinstance(health, Mapping):
        return False
    return fence.get("fenced") is True and health.get("healthy") is True


def _bounded_text(value: bytes, limit: int = 4096) -> str:
    return value.decode("utf-8", errors="replace")[:limit]
