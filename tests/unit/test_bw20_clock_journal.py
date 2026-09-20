# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hcuopt.deployment import bw20_clock_journal
from hcuopt.deployment.bw20_clock_journal import ClockJournal, ClockJournalError


def begin(journal):
    return journal.begin(resource_id="fixture", authorization_id="test-only",
                         original={"mode": "auto"})


def test_resource_claim_is_atomic_and_stale_transitions_fail(tmp_path):
    journal = ClockJournal(tmp_path / "clock.sqlite")

    def claim(_):
        try:
            return begin(journal)
        except ClockJournalError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = [value for value in pool.map(claim, range(4)) if value]
    assert len(claimed) == 1
    operation = claimed[0]
    with pytest.raises(ClockJournalError):
        journal.transition(operation, expected="mutation_possible", state="restored")
    journal.transition(operation, expected="mutation_possible", state="restoring")
    with pytest.raises(ClockJournalError):
        journal.transition(operation, expected="mutation_possible", state="active")
    journal.transition(operation, expected="restoring", state="restored")
    assert not journal.unresolved("fixture")
    assert begin(journal) != operation


def test_intent_survives_abrupt_subprocess_exit(tmp_path):
    path = tmp_path / "clock.sqlite"
    code = """
import os, sys
from pathlib import Path
from hcuopt.deployment.bw20_clock_journal import ClockJournal
journal = ClockJournal(Path(sys.argv[1]))
journal.begin(resource_id='fixture', authorization_id='test-only', original={'mode':'auto'})
os._exit(17)
"""
    # Pin the child to the same source tree, including uploaded no-Git snapshots.
    source_root = Path(bw20_clock_journal.__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(source_root)}
    result = subprocess.run([sys.executable, "-c", code, str(path)],
                            env=env, timeout=20, check=False)
    assert result.returncode == 17
    reopened = ClockJournal(path)
    assert reopened.unresolved("fixture")[0]["state"] == "mutation_possible"
    with pytest.raises(ClockJournalError):
        begin(reopened)
