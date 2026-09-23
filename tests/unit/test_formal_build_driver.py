# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from pathlib import Path
from types import SimpleNamespace

import pytest

from hcuopt.deployment.formal_build_driver import FormalBuildDriver


@pytest.mark.parametrize("budget", [True, 0, -1, float("nan"), float("inf"), "60"])
def test_invalid_budget_rejected_before_runtime_access(budget):
    driver = FormalBuildDriver(SimpleNamespace(), artifact_root=Path("unused"),
                               cache_root=Path("unused"), output_root=Path("unused"))
    with pytest.raises(ValueError, match="finite and positive"):
        driver.build_candidate(intent_id=None, worker_id="x", claim_token=None,
                               candidate_id=None, wall_seconds=budget)


@pytest.mark.parametrize("ttl", [True, 0, 301, "60"])
def test_invalid_ttl_rejected_before_dispatch(ttl):
    driver = FormalBuildDriver(SimpleNamespace(), artifact_root=Path("unused"),
                               cache_root=Path("unused"), output_root=Path("unused"))
    with pytest.raises(ValueError, match="TTL"):
        driver.start(None, "worker", ttl_seconds=ttl)
