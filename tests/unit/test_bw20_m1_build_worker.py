# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from pathlib import Path

import pytest

from hcuopt.adapters.profiles import BW20_MANUAL_CANDIDATE_PROFILE
from hcuopt.deployment.bw20_m1_build_worker import (
    BW20M1BuildJobHandler,
    build_bw20_m1_source_registry,
)
from hcuopt.domain.errors import ExecutionSafetyError


def test_build_registry_exposes_only_real_c_line_capabilities(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    output = tmp_path / "output"
    packages.mkdir()

    registry = build_bw20_m1_source_registry(
        source_package_root=packages,
        output_dir=output,
    )

    assert registry.profile == BW20_MANUAL_CANDIDATE_PROFILE
    assert set(registry.available()) == {
        "candidate_builder",
        "source_manager",
        "artifact_store",
    }
    builder = registry.require("candidate_builder")
    assert builder.provenance.profile == BW20_MANUAL_CANDIDATE_PROFILE
    assert builder.provenance.implementation_kind == "real"


def test_build_registry_rejects_overlapping_trust_roots(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()

    with pytest.raises(ExecutionSafetyError, match="must be separate"):
        build_bw20_m1_source_registry(
            source_package_root=packages,
            output_dir=packages / "output",
        )


def test_handler_rejects_every_non_manual_build_job(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    registry = build_bw20_m1_source_registry(
        source_package_root=packages,
        output_dir=tmp_path / "output",
    )
    handler = BW20M1BuildJobHandler(registry, tmp_path / "output")

    for job_type in ("source_prepare", "noop_build", "manual_correctness", "shell"):
        with pytest.raises(ExecutionSafetyError, match="only manual_build"):
            handler.handle(job_type, {})
