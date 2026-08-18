"""External tool adapters. Core domain objects must not depend on vendor schemas."""

from hcuopt.adapters.execution import ContainerExecutionAdapter, SSHExecutionAdapter
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.adapters.resource_cleaner import ContainerResourceCleaner

__all__ = [
    "AdapterRegistry",
    "ContainerExecutionAdapter",
    "ContainerResourceCleaner",
    "SSHExecutionAdapter",
]
