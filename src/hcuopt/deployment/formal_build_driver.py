# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Bounded CPU build orchestration; never grants a resource lease or runs HCU."""

import math
from pathlib import Path
from uuid import UUID, uuid5

from hcuopt.contracts.m2 import BudgetUsage, RoundBudgetLedgerEntry, RoundBudgetReservation
from hcuopt.domain.errors import Conflict
from hcuopt.storage.formal_build_jobs import PostgresFormalBuildJobs


class FormalBuildDriver:
    def __init__(self, runtime, *, artifact_root: Path, cache_root: Path, output_root: Path):
        self.runtime = runtime
        self.artifact_root = artifact_root
        self.cache_root, self.output_root = cache_root, output_root

    def start(self, intent_id: UUID, worker_id: str, *, ttl_seconds: int = 300):
        """Explicit dispatch and one-shot claim; retain returned token privately.

        A second call never steals/reuses a claim. A lost claim response needs
        reconciliation; do not infer ownership from the worker name alone.
        Worker registration remains an existing deployment prerequisite.
        """
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise ValueError("claim TTL must be 1-300 seconds")
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 128:
            raise ValueError("worker_id must contain 1-128 characters")
        self.runtime.dispatcher.create(intent_id)
        return self.runtime.claims.claim(intent_id, worker_id, ttl_seconds=ttl_seconds)

    def build_candidate(
        self, *, intent_id: UUID, worker_id: str, claim_token: UUID,
        candidate_id: UUID, wall_seconds: float,
    ):
        """Reserve once, build once, then settle/publish through the existing journal.

        Budget is accounting, not a kill timeout. Claim/stop/window checks remain
        enforced; overrun or uncertain work is not automatically retried.
        """
        if (isinstance(wall_seconds, bool) or not isinstance(wall_seconds, (int, float))
                or not math.isfinite(wall_seconds) or wall_seconds <= 0):
            raise ValueError("build wall budget must be finite and positive")
        runtime, repo = self.runtime, self.runtime.repository
        runtime.claims.assert_active(intent_id, worker_id, claim_token)
        intent = repo.get_formal_start_intent(intent_id)
        if not any(member.candidate_id == candidate_id for member in intent.candidate_bindings):
            raise Conflict("Formal build candidate is outside Intent")
        with repo.connection() as conn:
            round_ = conn.execute(
                "SELECT adapter_profile FROM search_rounds WHERE round_id = %s", (intent.round_id,),
            ).fetchone()
            worker = conn.execute(
                "SELECT worker_type, adapter_profile FROM workers WHERE worker_id = %s",
                (worker_id,),
            ).fetchone()
            member = conn.execute(
                "SELECT source_package_hash FROM round_candidates "
                "WHERE round_id = %s AND candidate_id = %s", (intent.round_id, candidate_id),
            ).fetchone()
        if (worker is None or round_ is None or member is None or worker["worker_type"] != "build"
                or worker["adapter_profile"] != round_["adapter_profile"]):
            raise Conflict("Formal build requires a registered matching CPU Worker")
        job = PostgresFormalBuildJobs(runtime.claims, intent_id, worker_id, claim_token).enqueue(
            candidate_id,
        )
        reservation_id = uuid5(job["job_id"], "formal-driver-build-reservation-v1")
        usage = BudgetUsage(build_attempts=1, wall_seconds=wall_seconds)
        reservation = RoundBudgetReservation(
            reservation_id=reservation_id, round_id=intent.round_id, job_id=job["job_id"],
            attempt=1, candidate_id=candidate_id, planned=usage, state="reserved",
            idempotency_key=f"formal-driver-build:{job['job_id']}",
        )
        entry = RoundBudgetLedgerEntry(
            ledger_entry_id=uuid5(reservation_id, "reserve"), reservation_id=reservation_id,
            round_id=intent.round_id, entry_type="reserve", reserved=usage, actual=BudgetUsage(),
            lease_held_seconds=0, harness_active_seconds=0,
            raw_usage_evidence_hash=member["source_package_hash"],
            idempotency_key=f"formal-driver-reserve:{job['job_id']}", created_at=job["created_at"],
        )
        repo.reserve_round_budget(reservation, entry)
        consumer = runtime.local_build_consumer(
            intent_id=intent_id, worker_id=worker_id, claim_token=claim_token,
            artifact_root=self.artifact_root, cache_root=self.cache_root,
        )
        return consumer.execute_current_once(
            reservation_id=reservation_id, output_dir=self.output_root / str(intent_id),
        )

    def build_family(self, *, intent_id, worker_id, claim_token, wall_seconds_per_candidate):
        """Visit only the signed Intent's bounded family; abort on first failure."""
        intent = self.runtime.repository.get_formal_start_intent(intent_id)
        return tuple(self.build_candidate(
            intent_id=intent_id, worker_id=worker_id, claim_token=claim_token,
            candidate_id=member.candidate_id, wall_seconds=wall_seconds_per_candidate,
        ) for member in intent.candidate_bindings)
