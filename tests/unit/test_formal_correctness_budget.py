# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from unittest.mock import Mock
from uuid import uuid4

import pytest

from hcuopt.storage.formal_correctness_jobs import PostgresFormalCorrectnessJobs


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan"), True, "30", None])
def test_invalid_budget_does_not_create_job(seconds):
    claims = Mock(spec=["assert_active"])
    jobs = PostgresFormalCorrectnessJobs(claims, uuid4(), "owner", uuid4())
    with pytest.raises(ValueError, match="positive and finite"):
        jobs.reserve(uuid4(), wall_seconds=seconds)
    claims.assert_active.assert_not_called()
