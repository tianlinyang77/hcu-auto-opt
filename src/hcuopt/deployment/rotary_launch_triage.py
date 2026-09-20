# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Offline log annotation, not a correctness evaluator or an execution adapter."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

WARNING = re.compile(
    r"Launch params \((\d+),\s*(\d+),\s*(\d+)\) are larger than launch bounds "
    r"\((\d+)\) for kernel (\S*rotary_embedding_kernel\S*)"
)


def analyze(log: bytes, model_config: bytes) -> dict[str, object]:
    config = json.loads(model_config)
    observations = []
    for match in WARNING.finditer(log.decode("utf-8", errors="replace")):
        x, y, z, bound = map(int, match.groups()[:4])
        observations.append({
            "block": [x, y, z], "threads_per_block": x * y * z,
            "reported_compiled_bound": bound, "kernel_symbol": match.group(5),
        })
    hypothesis = None
    heads = config.get("num_attention_heads")
    hidden = config.get("hidden_size")
    if (config.get("model_type") == "qwen2" and type(heads) is int and heads > 0
            and type(hidden) is int and hidden > 0 and hidden % heads == 0):
        head_dim = config.get("head_dim", hidden // heads)
        if type(head_dim) is int and head_dim > 0 and head_dim % 2 == 0:
            block_x = min(heads * head_dim // 2, 512)
            hypothesis = {
                "basis": "frozen SGLang Qwen2 full rotary dimension, TP=1 only",
                "head_dim": head_dim, "num_heads": heads, "predicted_block_x": block_x,
                "matches_observed_block": any(o["block"] == [block_x, 1, 1]
                                              for o in observations),
                "installed_binary_source_binding": "not_verified",
            }
    return {
        "purpose": "offline_rotary_launch_triage",
        "log_sha256": hashlib.sha256(log).hexdigest(),
        "model_config_sha256": hashlib.sha256(model_config).hexdigest(),
        "status": "warning_observed" if observations else "no_matching_warning_observed",
        "observations": observations, "source_hypothesis": hypothesis,
        "correctness": "not_validated", "performance_conclusion": "not_measured",
        "release_allowed": False,
        "required_followup": [
            "bind installed sgl-kernel library hash and build provenance",
            "query actual function launch attributes on the locked device",
            "compare eager and graph results to an independent reference",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--model-config", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(analyze(args.log.read_bytes(), args.model_config.read_bytes()), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
