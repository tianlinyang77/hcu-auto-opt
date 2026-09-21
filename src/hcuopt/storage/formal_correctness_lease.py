# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""One-shot Formal correctness ownership on the existing Job/resource tables.

This is a control-plane lease, not proof of physical isolation. No process is
started, expired resources are never recycled, and unknown attempts cannot retry.
"""

import math
from datetime import timedelta
from uuid import UUID, uuid4

from hcuopt.contracts.m2 import BudgetUsage
from hcuopt.domain.errors import Conflict
from hcuopt.operator.formal_dispatch import prepare_formal_round
from hcuopt.storage.formal_correctness_jobs import PostgresFormalCorrectnessJobs
from hcuopt.storage.formal_dispatch import _insert


class PostgresFormalCorrectnessLease:
    def __init__(self, jobs: PostgresFormalCorrectnessJobs, *, enabled: bool = False):
        self.jobs, self.enabled = jobs, enabled

    def _prepared(self):
        if not self.enabled:
            raise Conflict("Formal correctness lease is disabled")
        jobs = self.jobs
        jobs.claims.assert_active(jobs.intent_id, jobs.worker_id, jobs.claim_token)
        dispatcher = jobs.claims.dispatcher
        return prepare_formal_round(
            dispatcher.coordinator,
            dispatcher.repository.get_formal_start_intent(jobs.intent_id),
            dispatcher.repository,
        )

    def _lock_authority(self, conn, prepared):
        jobs = self.jobs
        intent = jobs.claims._lock_deployment_intent(conn, jobs.intent_id)
        if intent != prepared.intent:
            raise Conflict("Formal correctness Intent changed")
        jobs.claims.dispatcher._revalidate_locked(conn, prepared)
        claim = conn.execute(
            "SELECT * FROM formal_dispatch_claims WHERE intent_id = %s AND worker_id = %s "
            "AND claim_token = %s AND state = 'claimed' AND expires_at > clock_timestamp()",
            (jobs.intent_id, jobs.worker_id, jobs.claim_token),
        ).fetchone()
        if claim is None or conn.execute(
            "SELECT 1 FROM formal_dispatch_stop_requests WHERE intent_id = %s", (jobs.intent_id,),
        ).fetchone():
            raise Conflict("Formal correctness requires a live unstopped claim")
        return claim

    def claim(self, candidate_id: UUID, reservation_id: UUID, *, executor_id: str) -> dict:
        """Atomically claim the reserved Job and the exact authorized resource.

        Repeated claims reject, including lost responses. Recovery must inspect
        the retained running Job instead of invoking another attempt.
        """
        prepared = self._prepared()
        queued = self.jobs.enqueue(candidate_id)
        repo = self.jobs.claims.dispatcher.repository
        plan = prepared.preview.resolved_plan
        with repo.connection() as conn:
            owner = self._lock_authority(conn, prepared)
            # Preserve the Round -> reservation -> Job lock order used by budgets.
            round_ = conn.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (prepared.intent.round_id,),
            ).fetchone()
            budget = conn.execute(
                "SELECT * FROM round_budget_reservations WHERE reservation_id = %s FOR UPDATE",
                (reservation_id,),
            ).fetchone()
            job = conn.execute("SELECT * FROM jobs WHERE job_id = %s FOR UPDATE",
                               (queued["job_id"],)).fetchone()
            if (job is None or job["state"] != "queued" or job["attempts"] != 0
                    or job["payload"] != queued["payload"] or job["execution_lane"] != "formal"
                    or job["job_type"] != "manual_correctness" or job["max_attempts"] != 1
                    or job["lease_scope"] != "shared" or job["accepted_worker_type"] != "gpu"):
                raise Conflict(
                    "Formal correctness Job already started or changed; recovery required"
                )
            if (round_ is None or round_["state"] != "correctness"
                    or round_["artifact_family_hash"] != job["payload"]["artifact_family_hash"]):
                raise Conflict("Formal correctness frozen Round changed")
            if (budget is None or budget["job_id"] != job["job_id"]
                    or budget["round_id"] != prepared.intent.round_id
                    or budget["candidate_id"] != candidate_id or budget["attempt"] != 1
                    or budget["phase"] is not None or budget["state"] != "reserved"):
                raise Conflict("Formal correctness requires its reserved budget")
            usage = BudgetUsage.model_validate(budget["planned"])
            expected_usage = BudgetUsage(correctness_attempts=1, wall_seconds=usage.wall_seconds)
            if (not math.isfinite(usage.wall_seconds) or usage.wall_seconds <= 0
                    or usage != expected_usage):
                raise Conflict("Formal correctness budget is not one bounded attempt")
            worker = conn.execute("SELECT * FROM workers WHERE worker_id = %s FOR SHARE",
                                  (executor_id,)).fetchone()
            if (worker is None or worker["worker_type"] != "gpu" or worker["state"] != "online"
                    or worker["adapter_profile"] != job["adapter_profile"]
                    or worker["capabilities"].get("host_id") != plan.authorized_host_id
                    or worker["capabilities"].get("resource_id") != plan.authorized_resource_id):
                raise Conflict("Formal correctness executor differs from authorized host/resource")
            resource = conn.execute(
                "SELECT * FROM resources WHERE resource_id = %s FOR UPDATE",
                (plan.authorized_resource_id,),
            ).fetchone()
            if (resource is None or resource["state"] != "available"
                    or resource["owner_job_id"] is not None or resource["lease_id"] is not None):
                raise Conflict("Formal correctness resource is unavailable; no reclamation")
            now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            expires = min(now + timedelta(seconds=usage.wall_seconds), owner["expires_at"],
                          prepared.valid_until)
            if expires <= now:
                raise Conflict("Formal correctness window expired")
            lease_id, token = uuid4(), uuid4()
            fence = resource["fencing_token"] + 1
            conn.execute(
                "UPDATE resources SET state = 'active', owner_job_id = %s, lease_id = %s, "
                "fencing_token = %s, expires_at = %s, updated_at = clock_timestamp() "
                "WHERE resource_id = %s",
                (job["job_id"], lease_id, fence, expires, resource["resource_id"]),
            )
            result = conn.execute(
                "UPDATE jobs SET state = 'running', attempts = 1, "
                "claimed_by = %s, claim_token = %s, "
                "lease_id = %s, resource_id = %s, fencing_token = %s, claimed_at = %s, "
                "heartbeat_at = %s, updated_at = %s WHERE job_id = %s RETURNING *",
                (executor_id, token, lease_id, resource["resource_id"], fence, now, now, now,
                 job["job_id"]),
            ).fetchone()
            _insert(conn, "job_events", {
                "job_id": job["job_id"], "event_type": "formal_correctness_claimed",
                "details": {"reservation_id": str(reservation_id), "lease_id": str(lease_id),
                            "fencing_token": fence, "executor_id": executor_id,
                            "expires_at": expires.isoformat(),
                            "host_id": plan.authorized_host_id},
            })
            return dict(result)

    def assert_live(self, job_id: UUID, *, executor_id: str, token: UUID,
                    lease_id: UUID, fencing_token: int) -> None:
        """Read-only checkpoint. Never renews or interprets expiry as cleanup."""
        prepared = self._prepared()
        with self.jobs.claims.dispatcher.repository.connection() as conn:
            self._lock_authority(conn, prepared)
            round_ = conn.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR SHARE",
                (prepared.intent.round_id,),
            ).fetchone()
            budget = conn.execute(
                "SELECT * FROM round_budget_reservations WHERE job_id = %s AND attempt = 1 "
                "FOR SHARE", (job_id,),
            ).fetchone()
            job = conn.execute("SELECT * FROM jobs WHERE job_id = %s FOR UPDATE",
                               (job_id,)).fetchone()
            if (job is None or job["execution_lane"] != "formal"
                    or job["job_type"] != "manual_correctness" or job["state"] != "running"
                    or job["payload"].get("intent_id") != str(self.jobs.intent_id)
                    or job["payload"].get("worker_id") != self.jobs.worker_id
                    or job["payload"].get("claim_token") != str(self.jobs.claim_token)
                    or job["task_id"] != prepared.intent.task_id
                    or job["attempts"] != 1 or job["lease_scope"] != "shared"
                    or job["claimed_by"] != executor_id or job["claim_token"] != token
                    or job["lease_id"] != lease_id or job["fencing_token"] != fencing_token):
                raise Conflict("Formal correctness execution owner is stale")
            if (round_ is None or round_["state"] != "correctness"
                    or round_["artifact_family_hash"] != job["payload"]["artifact_family_hash"]
                    or budget is None or budget["state"] != "reserved"
                    or budget["round_id"] != prepared.intent.round_id
                    or str(budget["candidate_id"]) != job["payload"]["candidate_id"]
                    or job["resource_id"] != prepared.preview.resolved_plan.authorized_resource_id):
                raise Conflict("Formal correctness phase or reserved budget changed")
            resource = conn.execute(
                "SELECT *, expires_at > clock_timestamp() AS live FROM resources "
                "WHERE resource_id = %s FOR UPDATE", (job["resource_id"],),
            ).fetchone()
            if (resource is None or resource["state"] != "active" or not resource["live"]
                    or resource["owner_job_id"] != job_id or resource["lease_id"] != lease_id
                    or resource["fencing_token"] != fencing_token):
                raise Conflict("Formal correctness resource lease is stale; recovery required")
