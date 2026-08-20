"""External tool adapters. Core domain objects must not depend on vendor schemas."""

from hcuopt.adapters.execution import ContainerExecutionAdapter, SSHExecutionAdapter
from hcuopt.adapters.real_profile import (
    build_nmz36_framework_smoke_registry,
    build_nmz36_runtime_probe_registry,
    build_nmz36_stage0_measurement_registry,
    compose_nmz36_stage0_registry,
)
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.adapters.resource_cleaner import ContainerResourceCleaner

__all__ = [
    "AdapterRegistry",
    "ContainerExecutionAdapter",
    "ContainerResourceCleaner",
    "SSHExecutionAdapter",
    "build_nmz36_framework_smoke_registry",
    "build_nmz36_runtime_probe_registry",
    "build_nmz36_stage0_measurement_registry",
    "compose_nmz36_stage0_registry",
]
