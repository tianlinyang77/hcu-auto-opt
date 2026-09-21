# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Dedicated CPU build Job identity; never consumed by the general Worker loop."""

from uuid import UUID, uuid4

from hcuopt.domain.enums import WorkerType
from hcuopt.domain.errors import Conflict
from hcuopt.operator.formal_dispatch import prepare_formal_round
from hcuopt.storage.formal_claim import PostgresFormalClaimStore
from hcuopt.storage.formal_dispatch import _insert


class PostgresFormalBuildJobs:
    def __init__(self, claims: PostgresFormalClaimStore, intent_id: UUID,
                 worker_id: str, claim_token: UUID):
        self.claims, self.intent_id = claims, intent_id
        self.worker_id, self.claim_token = worker_id, claim_token

    def binding(self, candidate_id: UUID) -> dict:
        return {"intent_id": str(self.intent_id), "candidate_id": str(candidate_id),
                "worker_id": self.worker_id, "claim_token": str(self.claim_token)}

    def enqueue(self, candidate_id: UUID) -> dict:
        """Create an isolated queued Job; the original Budget API reserves before claim."""
        claims = self.claims
        claims.assert_active(self.intent_id, self.worker_id, self.claim_token)
        dispatcher, repo = claims.dispatcher, claims.dispatcher.repository
        prepared = prepare_formal_round(
            dispatcher.coordinator, repo.get_formal_start_intent(self.intent_id), repo,
        )
        with repo.connection() as conn:
            intent = claims._lock_deployment_intent(conn, self.intent_id)
            if intent != prepared.intent or not any(
                item.candidate_id == candidate_id for item in intent.candidate_bindings
            ):
                raise Conflict("Formal build Job differs from claimed Intent")
            dispatcher._revalidate_locked(conn, prepared)
            live = conn.execute(
                "SELECT 1 FROM formal_dispatch_claims WHERE intent_id = %s AND worker_id = %s "
                "AND claim_token = %s AND state = 'claimed' AND expires_at > clock_timestamp()",
                (self.intent_id, self.worker_id, self.claim_token),
            ).fetchone()
            if live is None or conn.execute(
                "SELECT 1 FROM formal_dispatch_stop_requests WHERE intent_id = %s",
                (self.intent_id,),
            ).fetchone():
                raise Conflict("Formal build Job requires a live unstopped claim")
            key = f"formal-build-job:{self.intent_id}:{candidate_id}"
            existing = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key = %s FOR UPDATE", (key,),
            ).fetchone()
            expected = {
                "task_id": intent.task_id, "job_type": "manual_build",
                "accepted_worker_type": WorkerType.BUILD.value, "execution_lane": "formal",
                "adapter_profile": prepared.round.adapter_profile, "lease_scope": "none",
                "payload": self.binding(candidate_id), "max_attempts": 1,
            }
            if existing is not None:
                if any(existing[k] != v for k, v in expected.items()):
                    raise Conflict("Formal build Job already binds another owner or input")
                return dict(existing)
            member = conn.execute(
                "SELECT state FROM round_candidates WHERE round_id = %s AND candidate_id = %s",
                (intent.round_id, candidate_id),
            ).fetchone()
            if member is None or member["state"] != "intake_accepted":
                raise Conflict("Formal build Job requires an unbuilt member")
            job_id = uuid4()
            _insert(conn, "jobs", {**expected, "job_id": job_id, "idempotency_key": key})
            _insert(conn, "job_events", {
                "job_id": job_id, "event_type": "formal_build_queued",
                "details": self.binding(candidate_id),
            })
            return dict(conn.execute("SELECT * FROM jobs WHERE job_id = %s", (job_id,)).fetchone())

    def _claim_reserved(self, conn, reservation: dict) -> None:
        """Called only inside journal.begin's locked transaction before invoking is committed."""
        job = conn.execute(
            "SELECT * FROM jobs WHERE job_id = %s FOR UPDATE", (reservation["job_id"],),
        ).fetchone()
        if job is None or (
            job["execution_lane"] != "formal" or job["job_type"] != "manual_build"
            or job["accepted_worker_type"] != WorkerType.BUILD.value
            or job["payload"] != self.binding(reservation["candidate_id"])
            or job["state"] != "queued" or job["attempts"] != 0 or reservation["attempt"] != 1
            or job["max_attempts"] != 1 or job["lease_scope"] != "none"
        ):
            raise Conflict("Formal build requires its isolated queued Job Attempt")
        worker = conn.execute(
            "SELECT * FROM workers WHERE worker_id = %s FOR SHARE", (self.worker_id,),
        ).fetchone()
        if worker is None or (
            worker["worker_type"] != WorkerType.BUILD.value
            or worker["adapter_profile"] != job["adapter_profile"]
        ):
            raise Conflict("Formal build requires a registered matching CPU Worker")
        conn.execute(
            "UPDATE jobs SET state = 'running', attempts = 1, claimed_by = %s, claim_token = %s, "
            "claimed_at = clock_timestamp(), heartbeat_at = clock_timestamp(), "
            "updated_at = clock_timestamp() WHERE job_id = %s",
            (self.worker_id, self.claim_token, job["job_id"]),
        )
        _insert(conn, "job_events", {
            "job_id": job["job_id"], "event_type": "formal_build_claimed",
            "details": self.binding(reservation["candidate_id"]),
        })
