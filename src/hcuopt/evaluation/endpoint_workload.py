# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Load an exact endpoint workload document and bind its byte identity."""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from hcuopt.measurement.endpoint_models import EndpointWorkloadSpec


def load_endpoint_workload_spec(path: Path) -> EndpointWorkloadSpec:
    raw_bytes = path.read_bytes()
    raw = yaml.safe_load(raw_bytes)
    if not isinstance(raw, dict):
        raise ValueError("endpoint workload document must be an object")
    if "workload_hash" in raw or "prompt_sha256" in raw:
        raise ValueError("endpoint workload identities are derived from the frozen document")
    prompt = raw.get("prompt")
    if not isinstance(prompt, str):
        raise ValueError("endpoint workload requires a prompt")
    raw["workload_hash"] = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
    raw["prompt_sha256"] = "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return EndpointWorkloadSpec.model_validate(raw)


__all__ = ["load_endpoint_workload_spec"]
