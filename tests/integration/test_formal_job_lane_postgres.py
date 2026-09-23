# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import os
from uuid import uuid4

import psycopg
import pytest

from hcuopt.contracts.v1 import JobCreate
from hcuopt.domain.errors import Conflict
from tests.integration.test_formal_build_journal_postgres import journal_case  # noqa: F401
from tests.integration.test_formal_build_postgres import build_record_case  # noqa: F401
from tests.integration.test_formal_dispatch_postgres import dispatch_case  # noqa: F401
from tests.integration.test_formal_start_management_postgres import isolated_dsn  # noqa: F401

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.getenv("HCUOPT_DATABASE_URL"), reason="requires PostgreSQL"),
]


def test_general_worker_and_recovery_cannot_consume_formal_job(journal_case):  # noqa: F811
    journal, reservation, result, _ = journal_case
    repo = journal.claims.dispatcher.repository
    intent = repo.get_formal_start_intent(journal.intent_id)
    ordinary = repo.enqueue_job(JobCreate(
        task_id=intent.task_id, job_type="manual_build", accepted_worker_type="build",
        idempotency_key=f"ordinary-test:{uuid4()}",
    ))
    assert repo.claim_job("builder")["job_id"] == ordinary["job_id"]
    journal.begin(result.build.candidate_id, reservation.reservation_id, "sha256:" + "a" * 64)
    with repo.connection() as conn:
        conn.execute("UPDATE jobs SET heartbeat_at = now() - interval '1 hour'")
    recovered = repo.recover_stale_jobs()
    assert ordinary["job_id"] in recovered
    assert reservation.job_id not in recovered
    with pytest.raises(Conflict, match="dedicated completion"):
        repo.complete_job(reservation.job_id, journal.claim_token, None, {})
    with repo.connection() as conn:
        assert conn.execute("SELECT state FROM jobs WHERE job_id = %s",
                            (reservation.job_id,)).fetchone()["state"] == "running"


def test_formal_lane_is_immutable_and_not_generically_advanced(journal_case):  # noqa: F811
    journal, reservation, _, _ = journal_case
    repo = journal.claims.dispatcher.repository
    with repo.connection() as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="lane is immutable"):
            conn.execute("UPDATE jobs SET execution_lane = 'general' WHERE job_id = %s",
                         (reservation.job_id,))
    with repo.connection() as conn:
        # Explicit terminal fixture to check only discovery, not completion authority.
        conn.execute("UPDATE jobs SET state = 'succeeded', finished_at = now() WHERE job_id = %s",
                     (reservation.job_id,))
    assert repo.unadvanced_succeeded_jobs() == []
