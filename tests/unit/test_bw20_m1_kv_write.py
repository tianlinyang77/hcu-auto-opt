from __future__ import annotations

from hcuopt.deployment.bw20_m1_kv_write import (
    KV_CASE_IDS,
    KV_REPLACEMENT_POINT,
    build_kv_write_hotspot_spec,
)
from hcuopt.measurement.m1_kv_write_reference import build_case, case_hash


def test_kv_write_spec_binds_full_cache_oracle_and_input_hashes() -> None:
    spec = build_kv_write_hotspot_spec("bw20-kv-write-test")
    assert spec.hotspot_id == "bw20-kv-write-test"
    assert tuple(case.case_id for case in spec.cases) == KV_CASE_IDS
    assert KV_REPLACEMENT_POINT.endswith("MHATokenToKVPool.set_kv_buffer")
    for correctness_case, expectation in zip(spec.cases, spec.input_expectations, strict=True):
        reference = build_case(correctness_case.case_id)
        assert expectation.input_hash == case_hash(reference)
        assert correctness_case.inputs[0].shape == (len(reference.locations),)
        assert correctness_case.outputs[0].shape == (
            reference.page_count,
            reference.head_count,
            64,
            reference.head_dim,
        )
        assert correctness_case.outputs[1].shape == (
            reference.page_count,
            reference.head_count,
            reference.head_dim,
            64,
        )
        assert all(output.atol == output.rtol == 0 for output in correctness_case.outputs)


def test_empty_case_is_not_silently_encoded_as_nonempty_tensor() -> None:
    spec = build_kv_write_hotspot_spec("bw20-kv-write-empty-boundary")
    assert "empty" not in {case.case_id for case in spec.cases}
    assert all(case.inputs[0].shape[0] > 0 for case in spec.cases)
