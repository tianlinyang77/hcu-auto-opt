# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Poll a trusted lease guard while a local Docker CLI is running.

Killing the CLI is not proof of container cleanup. The producer's finally block
must still fence its labeled containers and record health evidence.
"""

import math
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Event, Thread


def run_guarded_correctness_command(
    argv: Sequence[str], *, timeout: float, assert_live: Callable[[], None],
    stdout_path: Path, stderr_path: Path,
) -> subprocess.CompletedProcess:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("correctness timeout must be finite and positive")
    deadline = time.monotonic() + timeout

    def checkpoint():
        if time.monotonic() >= deadline:
            raise TimeoutError("correctness command deadline expired")
        # A stalled DB/connection must not stall the command's deadline. Only
        # this read-only callback runs in a daemon; never launch/release there.
        done = Event()
        outcome = []

        def check():
            try:
                outcome.append((True, assert_live()))
            except BaseException as error:
                outcome.append((False, error))
            finally:
                done.set()

        Thread(target=check, daemon=True, name="correctness-lease-check").start()
        if not done.wait(min(5.0, max(0.0, deadline - time.monotonic()))):
            raise TimeoutError("correctness lease check timed out")
        success, value = outcome[0]
        if not success:
            raise value
        if value is not None:
            raise RuntimeError("correctness lease guard must raise on failure")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("correctness command deadline expired")
        return remaining

    checkpoint()
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        process = subprocess.Popen(
            tuple(argv), stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, shell=False,
        )
        try:
            while True:
                remaining = checkpoint()
                try:
                    process.wait(timeout=min(0.5, remaining))
                except subprocess.TimeoutExpired:
                    continue
                checkpoint()
                break
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            raise
    return subprocess.CompletedProcess(
        tuple(argv), process.returncode, stdout_path.read_bytes(), stderr_path.read_bytes(),
    )
