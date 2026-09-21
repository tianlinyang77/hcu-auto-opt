# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Isolated correctness queue admission; never acquires a device or runs a verifier."""

from uuid import UUID, uuid4

from hcuopt.domain.errors import Conflict
from hcuopt.operator.formal_dispatch import prepare_formal_round
from hcuopt.orchestrator.search_round import artifact_family_hash
from hcuopt.storage.formal_claim import PostgresFormalClaimStore
from hcuopt.storage.formal_dispatch import _insert


class PostgresFormalCorrectnessJobs:
    def __init__(self, claims: PostgresFormalClaimStore, intent_id: UUID,
                 worker_id: str, claim_token: UUID):
        self.claims, self.intent_id = claims, intent_id
        self.worker_id, self.claim_token = worker_id, claim_token

    def enqueue(self, candidate_id: UUID) -> dict:
        """Bind a one-attempt Job to the complete frozen build family.

        Ownership here is control-plane ownership, not a resource lease. The
        future correctness consumer must reserve budget and acquire a shared
        lease before invoking D's producer. General Workers cannot claim this Job.
        """
        claims = self.claims
        claims.assert_active(self.intent_id, self.worker_id, self.claim_token)
        dispatcher, repo = claims.dispatcher, claims.dispatcher.repository
        prepared = prepare_formal_round(
            dispatcher.coordinator, repo.get_formal_start_intent(self.intent_id), repo,
        )
        with repo.connection() as conn:
            intent = claims._lock_deployment_intent(conn, self.intent_id)
            if intent != prepared.intent or not any(
                m.candidate_id == candidate_id for m in intent.candidate_bindings
            ):
                raise Conflict("Formal correctness Job differs from claimed Intent")
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
                raise Conflict("Formal correctness Job requires a live unstopped claim")
            round_ = conn.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s AND task_id = %s FOR UPDATE",
                (intent.round_id, intent.task_id),
            ).fetchone()
            if round_ is None or round_["run_mode"] != "formal" or (
                round_["state"] != "correctness" or not round_["artifact_family_hash"]
            ):
                raise Conflict("Formal correctness Job requires a frozen correctness Round")
            members = conn.execute(
                "SELECT * FROM round_candidates WHERE round_id = %s ORDER BY ordinal FOR SHARE",
                (intent.round_id,),
            ).fetchall()
            try:
                computed = artifact_family_hash(round_, members)
            except (KeyError, TypeError, ValueError) as error:
                raise Conflict("Formal correctness Build family is incomplete") from error
            if computed != round_["artifact_family_hash"]:
                raise Conflict("Formal correctness Build family differs")
            member = next((m for m in members if m["candidate_id"] == candidate_id), None)
            if (member is None or member["state"] != "built"
                    or member["candidate_kind"] != "business"):
                raise Conflict("Formal correctness Job requires a built business member")
            artifact = conn.execute(
                "SELECT 1 FROM artifacts WHERE artifact_id = %s AND candidate_id = %s "
                "AND task_id = %s AND content_hash = %s AND synthetic = FALSE FOR SHARE",
                (member["artifact_id"], candidate_id, intent.task_id, member["artifact_hash"]),
            ).fetchone()
            if artifact is None:
                raise Conflict("Formal correctness Job requires its durable real Artifact")
            payload = {
                "intent_id": str(self.intent_id), "candidate_id": str(candidate_id),
                "worker_id": self.worker_id, "claim_token": str(self.claim_token),
                "round_id": str(intent.round_id), "artifact_family_hash": computed,
                "artifact_id": str(member["artifact_id"]), "artifact_hash": member["artifact_hash"],
            }
            expected = {
                "task_id": intent.task_id, "job_type": "manual_correctness",
                "accepted_worker_type": "gpu", "execution_lane": "formal",
                "adapter_profile": round_["adapter_profile"], "lease_scope": "shared",
                "payload": payload, "max_attempts": 1,
            }
            key = f"formal-correctness-job:{self.intent_id}:{candidate_id}"
            existing = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key = %s FOR UPDATE", (key,),
            ).fetchone()
            if existing is not None:
                if any(existing[k] != v for k, v in expected.items()):
                    raise Conflict("Formal correctness Job already binds another input or owner")
                return dict(existing)
            job_id = uuid4()
            _insert(conn, "jobs", {**expected, "job_id": job_id, "idempotency_key": key})
            _insert(conn, "job_events", {
                "job_id": job_id, "event_type": "formal_correctness_queued", "details": payload,
            })
            return dict(conn.execute("SELECT * FROM jobs WHERE job_id = %s", (job_id,)).fetchone())
