# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hcuopt.deployment.nmz36_m1_allocator import (
    _base_docker_argv,
    build_m1_allocator_hotspot_spec,
)
from hcuopt.deployment.nmz36_m1_allocator import (
    _proc_start_token as deployment_proc_start_token,
)
from hcuopt.measurement.m1_allocator_reference import build_case, input_hash
from hcuopt.measurement.m1_allocator_worker import (
    _proc_start_token as worker_proc_start_token,
)
from hcuopt.measurement.m1_allocator_worker import _validate_expected_device
from hcuopt.targets import load_target

TARGET_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "targets"
    / "nmz36-sglang-0.5.12.yaml"
)


def _page_runs(values: tuple[int, ...]) -> tuple[int, ...]:
    pages = tuple(value // 64 for value in values)
    return tuple(
        page for ordinal, page in enumerate(pages) if ordinal == 0 or page != pages[ordinal - 1]
    )


def test_allocator_business_cases_free_each_page_in_one_contiguous_run() -> None:
    spec = build_m1_allocator_hotspot_spec("allocator-free-fixture")

    assert {case.case_id for case in spec.cases} == {
        "target-4091-direct-nosort",
        "target-4090-direct-nosort",
        "nonsorted-pages-direct-nosort",
        "sparse-pages-direct-nosort",
        "free-group-nosort",
        "nonsorted-pages-need-sort",
    }
    for case_spec in spec.cases:
        case = build_case(case_spec.case_id, 20260825, "ordinary")
        page_runs = _page_runs(case.values)
        assert len(page_runs) == len(set(page_runs))
        assert case_spec.inputs[0].shape == (len(case.values),)
        assert case_spec.outputs[0].shape == (len(case.unique_pages),)


def test_allocator_hotspot_spec_freezes_exact_input_hashes() -> None:
    spec = build_m1_allocator_hotspot_spec("allocator-free-fixture")
    expected = {
        item.case_id: item.input_hash for item in spec.input_expectations
    }

    for case_spec in spec.cases:
        case = build_case(case_spec.case_id, 20260825, "ordinary")
        assert expected[case_spec.case_id] == input_hash(case)
        assert case_spec.repeats == 2


def test_allocator_process_tokens_use_the_formal_linux_identity_format() -> None:
    process_id = 321
    stat_line = f"{process_id} (python worker) S " + " ".join(
        ["1"] * 18 + ["778899"]
    )

    expected = "linux-proc-startticks:778899"
    assert deployment_proc_start_token(stat_line, process_id) == expected
    assert worker_proc_start_token(stat_line, process_id) == expected


def test_allocator_worker_attests_optional_bw20_pci_and_architecture() -> None:
    properties = SimpleNamespace(
        pci_domain_id=0,
        pci_bus_id=177,
        pci_device_id=0,
        gcnArchName="gfx936:sramecc+",
    )
    torch = SimpleNamespace(
        cuda=SimpleNamespace(get_device_properties=lambda device: properties)
    )
    args = SimpleNamespace(
        expected_device_pci="0000:b1:00.0",
        expected_device_architecture="gfx936",
    )
    assert _validate_expected_device(torch, args) == {
        "pci": "0000:b1:00.0",
        "architecture": "gfx936",
        "logical_device_index": 0,
    }

    properties.pci_bus_id = 178
    with pytest.raises(RuntimeError, match="differs"):
        _validate_expected_device(torch, args)


def test_allocator_controller_keeps_docker_stdin_attached() -> None:
    argv = _base_docker_argv(
        target=load_target(TARGET_PATH),
        source_root=Path("/source"),
        evidence_dir=Path("/evidence"),
        cache_dir=Path("/cache"),
        container_name="m1-controller-fixture",
        resource_id="hcu-7",
        fencing_token=1,
        artifact=None,
    )

    assert argv[:4] == ["docker", "run", "--rm", "--interactive"]
