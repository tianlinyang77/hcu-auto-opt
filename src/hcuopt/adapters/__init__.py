"""External tool adapters. Core domain objects must not depend on vendor schemas."""

from hcuopt.adapters.registry import AdapterRegistry

__all__ = ["AdapterRegistry"]
