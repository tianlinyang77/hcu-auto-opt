"""Versioned contracts shared by the API, workers, and database boundary."""

from hcuopt.contracts.platform_v1 import PLATFORM_CONTRACT_VERSION
from hcuopt.contracts.v1 import CONTRACT_VERSION

M2_CONTRACT_VERSION = "m2a-v1"

__all__ = ["CONTRACT_VERSION", "M2_CONTRACT_VERSION", "PLATFORM_CONTRACT_VERSION"]
