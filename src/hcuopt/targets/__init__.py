"""Target lock loading and validation."""

from hcuopt.targets.fingerprint import target_fingerprint
from hcuopt.targets.loader import TargetCatalog, load_target

__all__ = ["TargetCatalog", "load_target", "target_fingerprint"]
