# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import math

import pytest

from hcuopt.deployment import bw20_rotary_correctness as probe


def test_neox_reference_uses_half_split_and_correct_rotation_sign():
    assert probe.reference_head([1, 2, 3, 4], [0, 0, 1, 1]) == [-3, -4, 1, 2]
    assert probe.reference_head([1, -2, 3, -4], [1, 1, 0, 0]) == [1, -2, 3, -4]


@pytest.mark.parametrize("values,cache", [([1], [1]), ([1, 2], [1, 2, 3, 4])])
def test_invalid_reference_shape_rejected(values, cache):
    with pytest.raises(ValueError):
        probe.reference_head(values, cache)


def test_reference_rotation_preserves_norm_with_unquantized_unit_cache():
    values = [0.2, -0.3, 0.7, -0.9]
    theta = .4
    result = probe.reference_head(values, [math.cos(theta)] * 2 + [math.sin(theta)] * 2)
    assert sum(v * v for v in result) == pytest.approx(sum(v * v for v in values), abs=1e-6)


def test_noop_and_wrong_layout_cannot_pass_reference_comparison():
    values = [1, 0.5, -1, -0.5]
    expected = probe.reference_head(values, [0, 0, 1, 1])
    assert not probe.metrics(values, expected)["passed"]
    assert not probe.metrics(list(reversed(expected)), expected)["passed"]
    assert probe.metrics(expected, expected)["passed"]


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_nonfinite_output_fails(value):
    assert probe.metrics([value], [0.0])["passed"] is False


def test_near_zero_uses_frozen_absolute_tolerance_and_reports_failures():
    assert probe.ATOL == probe.RTOL == 1 / 128
    assert probe.metrics([probe.ATOL / 2], [0])["passed"]
    result = probe.metrics([probe.ATOL * 2], [0])
    assert not result["passed"]
    assert result["mismatch_count"] == 1
    assert result["first_mismatches"][0]["index"] == 0


@pytest.mark.parametrize("actual,expected", [([], []), ([1], [1, 2])])
def test_missing_observations_fail(actual, expected):
    with pytest.raises(ValueError):
        probe.metrics(actual, expected)


def test_protocol_stops_before_loading_torch_on_wrong_python(monkeypatch, capsys):
    monkeypatch.setattr(probe.sys, "version_info", (3, 12))
    with pytest.raises(ValueError, match="Python3.10"):
        probe.main()
    lines = capsys.readouterr().out
    assert '"status": "failed"' in lines
    assert '"automatic_release_allowed": false' in lines


def test_emit_forbids_nan_in_evidence(capsys):
    with pytest.raises(ValueError):
        probe.emit({"invalid": math.nan})
    assert capsys.readouterr().out == ""
