# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Operator-pinned F1 admission evidence; never supplied by an API caller.

Hashes bind reviewed deployment inputs, not a signature or a human signoff.
This object grants no Stage0, optimization, or release capabilities.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from hcuopt.contracts.platform_v1 import TargetSpec
from hcuopt.deployment.bw20_build_worker import validate_target
from hcuopt.deployment.bw20_pair_prepare import regular_file
from hcuopt.deployment.bw20_runtime_binding import validate_binding
from hcuopt.deployment.bw20_smoke_preflight import MODEL
from hcuopt.evaluation.sglang_smoke import load_workload_spec
from hcuopt.targets import target_fingerprint


def pinned_bytes(path: Path, digest: str) -> bytes:
    data = regular_file(path).read_bytes()
    if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("operator-pinned admission evidence changed")
    return data


@dataclass(frozen=True)
class BW20SmokeAdmission:
    target_fingerprint: str
    runtime_evidence: Path
    runtime_evidence_sha256: str
    workload: Path
    workload_sha256: str

    def validate(self, target: TargetSpec) -> None:
        validate_target(target)
        if target_fingerprint(target) != self.target_fingerprint:
            raise ValueError("target is not the operator-pinned F1 revision")
        if target.stage0_status != "pending" or target.automatic_release_allowed:
            raise ValueError("F1 admission cannot carry Stage0 or release acceptance")
        record = json.loads(pinned_bytes(self.runtime_evidence, self.runtime_evidence_sha256))
        validate_binding(record)
        if (record["source_commit"] != target.source_baseline.commit
                or record["version"] != target.inference_image.sglang_package_version):
            raise ValueError("runtime evidence differs from target")
        workload_bytes = pinned_bytes(self.workload, self.workload_sha256)
        spec = load_workload_spec(self.workload)
        if self.workload.read_bytes() != workload_bytes:
            raise ValueError("workload changed during admission")
        if (spec.target_id != target.target_id or spec.workload_id != "bw20-sglang-smoke-v1"
                or spec.model_path != MODEL or spec.tensor_parallel_size != 1
                or spec.max_new_tokens != 8):
            raise ValueError("workload differs from the bounded F1 scope")
