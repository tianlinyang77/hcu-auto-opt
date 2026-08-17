"""Versioned contracts shared by the API, workers, and database boundary."""

from dcuopt.contracts.platform_v1 import PLATFORM_CONTRACT_VERSION
from dcuopt.contracts.v1 import CONTRACT_VERSION

__all__ = ["CONTRACT_VERSION", "PLATFORM_CONTRACT_VERSION"]
