# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from pathlib import Path

import pytest

from hcuopt.deployment.bw20_m1_correctness_worker import (
    BW20M1CorrectnessJobHandler,
)
from hcuopt.domain.errors import ExecutionSafetyError


class _Registry:
    profile = "bw20-m1-manual-v1"

    @staticmethod
    def require(capability: str) -> object:
        assert capability == "kernel_correctness"
        return object()


def test_correctness_handler_rejects_every_other_job(tmp_path: Path) -> None:
    handler = BW20M1CorrectnessJobHandler(_Registry(), tmp_path)  # type: ignore[arg-type]

    for job_type in ("manual_build", "manual_performance", "stage0_probe", "shell"):
        with pytest.raises(ExecutionSafetyError, match="only manual_correctness"):
            handler.handle(job_type, {})


def test_correctness_cleanup_rejects_cross_job_scope(tmp_path: Path) -> None:
    handler = BW20M1CorrectnessJobHandler(_Registry(), tmp_path)  # type: ignore[arg-type]

    with pytest.raises(ExecutionSafetyError, match="scope"):
        handler.cleanup("manual_performance", {})
