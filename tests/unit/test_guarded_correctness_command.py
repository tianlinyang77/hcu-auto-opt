# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import subprocess
import sys
from threading import Event
from unittest.mock import Mock

import pytest

from hcuopt.deployment.guarded_correctness_command import run_guarded_correctness_command


def paths(tmp_path):
    return dict(stdout_path=tmp_path / "stdout.log", stderr_path=tmp_path / "stderr.log")


def test_real_cpu_command_keeps_output(tmp_path):
    guard = Mock(return_value=None)
    result = run_guarded_correctness_command(
        [sys.executable, "-c", "print('cpu-only')"], timeout=10, assert_live=guard,
        **paths(tmp_path),
    )
    assert result.returncode == 0 and result.stdout.strip() == b"cpu-only"
    assert guard.call_count >= 3


@pytest.mark.parametrize("reason", ["stop", "lease expired", "database unavailable"])
def test_guard_failure_kills_owned_cpu_process(tmp_path, monkeypatch, reason):
    from hcuopt.deployment import guarded_correctness_command as module

    processes = []
    real_popen = subprocess.Popen

    def capture(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", capture)
    guard = Mock(side_effect=[None, None, RuntimeError(reason)])
    with pytest.raises(RuntimeError, match=reason):
        run_guarded_correctness_command(
            [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"],
            timeout=10, assert_live=guard, **paths(tmp_path),
        )
    assert len(processes) == 1 and processes[0].poll() is not None
    assert (tmp_path / "stdout.log").is_file()


def test_deadline_kills_owned_cpu_process(tmp_path, monkeypatch):
    from hcuopt.deployment import guarded_correctness_command as module

    process = Mock()
    process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 0.1), 0]
    process.poll.return_value = None
    monkeypatch.setattr(module.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(module.time, "monotonic", Mock(side_effect=[0, 0, 0, 0, 0, 0, 0.1, 2]))
    with pytest.raises(TimeoutError, match="deadline"):
        run_guarded_correctness_command(
            ["fixture-command"], timeout=1, assert_live=lambda: None, **paths(tmp_path),
        )
    process.kill.assert_called_once()
    assert process.wait.call_args.kwargs == {"timeout": 5}


def test_rejected_before_launch_and_no_output_overwrite(tmp_path, monkeypatch):
    from hcuopt.deployment import guarded_correctness_command as module

    launch = Mock()
    monkeypatch.setattr(module.subprocess, "Popen", launch)
    with pytest.raises(RuntimeError, match="stopped"):
        run_guarded_correctness_command(
            ["fixture-command"], timeout=1,
            assert_live=Mock(side_effect=RuntimeError("stopped")), **paths(tmp_path),
        )
    (tmp_path / "stdout.log").write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        run_guarded_correctness_command(
            ["fixture-command"], timeout=1, assert_live=lambda: None, **paths(tmp_path),
        )
    assert (tmp_path / "stdout.log").read_bytes() == b"existing"
    launch.assert_not_called()


def test_hung_guard_does_not_block_deadline(tmp_path):
    release = Event()
    try:
        with pytest.raises(TimeoutError, match="lease check"):
            run_guarded_correctness_command(
                ["must-not-launch"], timeout=0.05, assert_live=lambda: release.wait(10),
                **paths(tmp_path),
            )
    finally:
        release.set()
