# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from uuid import uuid4

import pytest

from hcuopt.deployment.bw20_auto_observation_session import (
    BW20AutoObservationSessionFactory,
)
from hcuopt.deployment.bw20_stage0_runtime import RESOURCE


def context(assert_live_lease=lambda: None):
    return {
        "resource_id": RESOURCE,
        "lease_scope": "exclusive",
        "fencing_token": 7,
        "lease_id": str(uuid4()),
        "job_id": str(uuid4()),
        "assert_live_lease": assert_live_lease,
    }


def test_auto_observation_session_never_requests_a_clock_write(tmp_path):
    session = BW20AutoObservationSessionFactory()(context(), tmp_path)
    with session:
        assert session.receipt()["restored"] is False
    receipt = session.receipt()
    assert receipt["policy"] == "host_auto_observe_only_v1"
    assert receipt["restored"] is True
    assert receipt["restoration_not_required"] is True
    assert receipt["clock_mutation_performed"] is False
    assert receipt["clock_backend_required"] is False
    assert receipt["quarantined"] is False
    assert receipt["stage0_accepted"] is False
    assert receipt["automatic_release_allowed"] is False


def test_lost_lease_at_exit_quarantines_without_clock_action(tmp_path):
    checks = 0

    def lease():
        nonlocal checks
        checks += 1
        return None if checks == 1 else False

    session = BW20AutoObservationSessionFactory()(context(lease), tmp_path)
    with pytest.raises(RuntimeError, match="live lease"):
        with session:
            pass
    assert session.receipt()["quarantined"] is True
    assert session.receipt()["clock_mutation_performed"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"resource_id": "other"},
        {"lease_scope": "none"},
        {"fencing_token": 0},
        {"fencing_token": True},
        {"assert_live_lease": None},
    ],
)
def test_factory_rejects_untrusted_or_wrong_scope(change, tmp_path):
    value = context()
    value.update(change)
    with pytest.raises(ValueError):
        BW20AutoObservationSessionFactory()(value, tmp_path)
