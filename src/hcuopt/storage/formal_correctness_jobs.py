# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Isolated correctness queue admission; never acquires a device or runs a verifier."""

import hashlib
import math
from uuid import UUID, uuid4, uuid5

from hcuopt.contracts.m2 import BudgetUsage, RoundBudgetLedgerEntry, RoundBudgetReservation
from hcuopt.domain.errors import Conflict
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.operator.formal_dispatch import prepare_formal_round
from hcuopt.orchestrator.search_round import artifact_family_hash
from hcuopt.storage.formal_claim import PostgresFormalClaimStore
from hcuopt.storage.formal_dispatch import _insert


class PostgresFormalCorrectnessJobs:
    def __init__(self, claims: PostgresFormalClaimStore, intent_id: UUID,
                 worker_id: str, claim_token: UUID):
        self.claims, self.intent_id = claims, intent_id
        self.worker_id, self.claim_token = worker_id, claim_token

    def reserve(self, candidate_id: UUID, *, wall_seconds: float) -> dict:
        """Reserve one correctness attempt through the existing Round ledger.

        This does not claim a device or invoke external work. A stop racing with
        the ledger transaction may leave reserved credit; the future consumer
        must recheck authority before running, and reconciliation releases credit.
        """
        if (isinstance(wall_seconds, bool) or not isinstance(wall_seconds, (int, float))
                or not math.isfinite(wall_seconds) or wall_seconds <= 0):
            raise ValueError("Formal correctness wall budget must be positive and finite")
        job = self.enqueue(candidate_id)
        if job["state"] != "queued" or job["attempts"] != 0:
            raise Conflict("Formal correctness reservation requires an unstarted Job")
        # The current resource table has one owner even for shared correctness.
        planned = BudgetUsage(correctness_attempts=1, wall_seconds=wall_seconds,
                              exclusive_lease_seconds=wall_seconds)
        reservation_id = uuid5(job["job_id"], "formal-correctness-reservation-v1")
        round_id = UUID(job["payload"]["round_id"])
        request = RoundBudgetReservation(
            reservation_id=reservation_id, round_id=round_id, job_id=job["job_id"],
            attempt=1, candidate_id=candidate_id, planned=planned, state="reserved",
            idempotency_key=f"formal-correctness-reserve:{job['job_id']}",
        )
        evidence_hash = "sha256:" + hashlib.sha256(canonical_json_bytes({
            "job_id": str(job["job_id"]), "binding": job["payload"],
            "planned": planned.model_dump(mode="json"),
        })).hexdigest()
        entry = RoundBudgetLedgerEntry(
            ledger_entry_id=uuid5(reservation_id, "reserve-v1"),
            reservation_id=reservation_id, round_id=round_id, entry_type="reserve",
            reserved=planned, actual=BudgetUsage(), lease_held_seconds=0,
            harness_active_seconds=0, raw_usage_evidence_hash=evidence_hash,
            idempotency_key=f"formal-correctness-reserve-ledger:{job['job_id']}",
            created_at=job["created_at"],
        )
        result = self.claims.dispatcher.repository.reserve_round_budget(request, entry)
        if result["reservation"]["state"] != "reserved":
            raise Conflict("Formal correctness budget already finalized; reconciliation required")
        return result

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
