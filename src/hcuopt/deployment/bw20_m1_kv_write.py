# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Unregistered correctness spec for the BW20 paged KV-write hypothesis.

This is an intake artifact, not a Real Adapter. Registering the profile still
requires a separate GPU correctness producer and matching measurement worker.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from hcuopt.evaluation.m1_protocol import (
    M1CorrectnessCase,
    M1HotspotCorrectnessSpec,
    M1InputExpectation,
    M1OutputSpec,
    M1TensorSpec,
)
from hcuopt.measurement.m1_kv_write_reference import build_case, case_hash

KV_RELATIVE_PATH = "python/sglang/srt/mem_cache/memory_pool.py"
KV_MOUNT_TARGET = "/usr/local/lib/python3.10/dist-packages/sglang/srt/mem_cache/memory_pool.py"
KV_REPLACEMENT_POINT = "sglang.srt.mem_cache.memory_pool.MHATokenToKVPool.set_kv_buffer"
KV_CASE_IDS = (
    "one",
    "boundary-63",
    "boundary-64",
    "boundary-65",
    "representative-128",
    "permuted",
)


def kv_reference_source_hash() -> str:
    from hcuopt.measurement import m1_kv_write_reference

    source = Path(m1_kv_write_reference.__file__).resolve(strict=True)
    if source.is_symlink() or not source.is_file():
        raise ValueError("KV-write reference must be a regular file")
    return "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()


def build_kv_write_hotspot_spec(hotspot_id: str) -> M1HotspotCorrectnessSpec:
    """Freeze small full-cache cases; empty and live-size cases need separate handling."""

    cases = []
    expectations = []
    for case_id in KV_CASE_IDS:
        case = build_case(case_id)
        n = len(case.locations)
        h = case.head_count
        d = case.head_dim
        p = case.page_count
        cases.append(
            M1CorrectnessCase(
                case_id=case_id,
                inputs=(
                    M1TensorSpec(name="loc", shape=(n,), dtype="int64"),
                    M1TensorSpec(name="cache_k", shape=(n, h, d), dtype="bfloat16"),
                    M1TensorSpec(name="cache_v", shape=(n, h, d), dtype="bfloat16"),
                ),
                outputs=(
                    M1OutputSpec(
                        name="full_k_cache",
                        shape=(p, h, 64, d),
                        dtype="bfloat16",
                        atol=0,
                        rtol=0,
                        equal_nan=False,
                    ),
                    M1OutputSpec(
                        name="full_v_cache",
                        shape=(p, h, d, 64),
                        dtype="bfloat16",
                        atol=0,
                        rtol=0,
                        equal_nan=False,
                    ),
                ),
                seeds=(20260924,),
                special_values=("ordinary",),
                repeats=2,
            )
        )
        expectations.append(
            M1InputExpectation(
                case_id=case_id,
                seed=20260924,
                special_value="ordinary",
                input_hash=case_hash(case),
            )
        )
    return M1HotspotCorrectnessSpec(
        hotspot_id=hotspot_id,
        reference_implementation=(
            "Independent full K/V cache oracle for distinct valid paged locations; "
            "untouched cells must equal the frozen sentinel. Capture, alternate streams, "
            "dtype conversion, and empty inputs remain separate fail-closed gates."
        ),
        reference_source_hash=kv_reference_source_hash(),
        cases=tuple(cases),
        input_expectations=tuple(expectations),
    )
