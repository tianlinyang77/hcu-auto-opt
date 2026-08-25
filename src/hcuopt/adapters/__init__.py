"""External tool adapters. Core domain objects must not depend on vendor schemas."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hcuopt.adapters.execution import ContainerExecutionAdapter, SSHExecutionAdapter
    from hcuopt.adapters.real_profile import (
        build_m1_source_artifact_registry,
        build_nmz36_framework_smoke_registry,
        build_nmz36_runtime_probe_registry,
        build_nmz36_stage0_measurement_registry,
        build_nmz36_stage0_registry,
        compose_nmz36_stage0_registry,
    )
    from hcuopt.adapters.registry import AdapterRegistry
    from hcuopt.adapters.resource_cleaner import ContainerResourceCleaner

__all__ = [
    "AdapterRegistry",
    "ContainerExecutionAdapter",
    "ContainerResourceCleaner",
    "SSHExecutionAdapter",
    "build_m1_source_artifact_registry",
    "build_nmz36_framework_smoke_registry",
    "build_nmz36_runtime_probe_registry",
    "build_nmz36_stage0_registry",
    "build_nmz36_stage0_measurement_registry",
    "compose_nmz36_stage0_registry",
]

_EXPORT_MODULES = {
    "AdapterRegistry": "hcuopt.adapters.registry",
    "ContainerExecutionAdapter": "hcuopt.adapters.execution",
    "ContainerResourceCleaner": "hcuopt.adapters.resource_cleaner",
    "SSHExecutionAdapter": "hcuopt.adapters.execution",
    "build_m1_source_artifact_registry": "hcuopt.adapters.real_profile",
    "build_nmz36_framework_smoke_registry": "hcuopt.adapters.real_profile",
    "build_nmz36_runtime_probe_registry": "hcuopt.adapters.real_profile",
    "build_nmz36_stage0_registry": "hcuopt.adapters.real_profile",
    "build_nmz36_stage0_measurement_registry": "hcuopt.adapters.real_profile",
    "compose_nmz36_stage0_registry": "hcuopt.adapters.real_profile",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted((*globals(), *_EXPORT_MODULES))
