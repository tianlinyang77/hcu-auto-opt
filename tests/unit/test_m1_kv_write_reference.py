from __future__ import annotations

import pytest

from hcuopt.measurement.m1_kv_write_reference import (
    PAGE_SIZE,
    KVWriteCase,
    build_case,
    case_hash,
    expected_full_cache,
    expected_writes,
    verify_full_cache,
)


@pytest.mark.parametrize(
    "case_id,size",
    [
        ("representative-128", 128),
        ("empty", 0),
        ("one", 1),
        ("boundary-63", 63),
        ("boundary-64", 64),
        ("boundary-65", 65),
        ("permuted", 5),
    ],
)
def test_reference_covers_bounded_cases(case_id: str, size: int) -> None:
    case = build_case(case_id)
    keys, values = expected_writes(case)
    assert len(case.locations) == size
    assert len(keys) == len(values) == size * case.head_count * case.head_dim
    assert all(0 <= index[0] < case.page_count for index in keys)
    assert all(0 <= index[0] < case.page_count for index in values)
    assert case_hash(case) == case_hash(build_case(case_id))


def test_reference_writes_k_and_v_at_different_layout_offsets() -> None:
    case = KVWriteCase(case_id="cross-page", locations=(PAGE_SIZE - 1, PAGE_SIZE), page_count=2)
    keys, values = expected_writes(case)
    assert (0, 1, 63, 7) in keys
    assert (1, 1, 0, 7) in keys
    assert (0, 1, 7, 63) in values
    assert (1, 1, 7, 0) in values
    assert keys[(0, 1, 63, 7)] != values[(0, 1, 7, 63)]


@pytest.mark.parametrize("locations", [(1, 1), (-1,), (4 * PAGE_SIZE,), (True,)])
def test_reference_rejects_invalid_or_ambiguous_locations(locations: tuple[int, ...]) -> None:
    with pytest.raises(ValueError):
        KVWriteCase(case_id="invalid", locations=locations, page_count=4)


def test_unknown_case_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_case("invented")


def test_full_cache_oracle_detects_untouched_and_layout_errors() -> None:
    case = KVWriteCase(case_id="tiny", locations=(63, 64), page_count=2, head_count=1, head_dim=2)
    keys, values = expected_full_cache(case)
    assert verify_full_cache(case, keys, values)
    assert keys[0] == values[0] == -127
    wrong_key = keys.copy()
    wrong_key[0] = 0
    assert not verify_full_cache(case, wrong_key, values)
    wrong_value = values.copy()
    wrong_value[1] = 0
    assert not verify_full_cache(case, keys, wrong_value)
    assert not verify_full_cache(case, keys[:-1], values)


def test_full_cache_oracle_rejects_production_sized_allocation() -> None:
    case = KVWriteCase(case_id="too-large", locations=(0,), page_count=72279)
    with pytest.raises(ValueError, match="cell budget"):
        expected_full_cache(case)
