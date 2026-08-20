import hashlib
import json

from hcuopt.contracts.platform_v1 import TargetSpec


def target_fingerprint(target: TargetSpec) -> str:
    encoded = json.dumps(
        target.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
