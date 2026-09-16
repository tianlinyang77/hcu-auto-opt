# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from pathlib import Path

import pytest

from hcuopt.deployment.bw20_m1_correctness_worker import (
    BW20M1CorrectnessJobHandler,
    BW20M1OutputAccess,
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


class _Runner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...], timeout: float):  # type: ignore[no-untyped-def]
        del timeout
        self.calls.append(argv)
        if argv[0] == "getfacl":
            return type("Result", (), {"returncode": 0, "stdout": b"user:65534:rwx\n"})()
        return type("Result", (), {"returncode": 0, "stdout": b""})()


def test_output_access_grants_only_a_directory_below_job_root(tmp_path: Path) -> None:
    root = tmp_path / "job"
    output = root / "variant"
    outside = tmp_path / "outside"
    output.mkdir(parents=True)
    outside.mkdir()
    runner = _Runner()
    access = BW20M1OutputAccess(root, runner)  # type: ignore[arg-type]

    access(output)

    assert runner.calls[0][:4] == ("setfacl", "-m", "u:65534:rwx", "--")
    with pytest.raises(ExecutionSafetyError, match="escaped"):
        access(outside)
