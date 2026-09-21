# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Durable one-shot correctness invocation using the existing Job audit stream.

All writers serialize on the owned Job. A recorded result is evidence retention,
not a correctness acceptance, resource release, or Round transition.
"""

import hashlib
import math
import re
from uuid import UUID, uuid5

from psycopg.types.json import Jsonb

from hcuopt.contracts.m2 import BudgetUsage, RoundBudgetLedgerEntry
from hcuopt.contracts.v1 import ManualCorrectnessResult
from hcuopt.domain.errors import Conflict
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.storage.formal_correctness_lease import PostgresFormalCorrectnessLease
from hcuopt.storage.formal_dispatch import _insert


class PostgresFormalCorrectnessJournal:
    def __init__(self, lease: PostgresFormalCorrectnessLease, job_id: UUID,
                 *, executor_id: str, token: UUID, lease_id: UUID, fencing_token: int):
        self.lease, self.job_id = lease, job_id
        self.owner = dict(executor_id=executor_id, token=token, lease_id=lease_id,
                          fencing_token=fencing_token)

    def _locked(self, conn, input_hash):
        if not self.lease.enabled:
            raise Conflict("Formal correctness journal is disabled")
        if not isinstance(input_hash, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", input_hash):
            raise ValueError("Formal correctness input Hash must be SHA256")
        jobs = self.lease.jobs
        intent = jobs.claims._lock_deployment_intent(conn, jobs.intent_id)
        job = conn.execute("SELECT * FROM jobs WHERE job_id = %s FOR UPDATE",
                           (self.job_id,)).fetchone()
        if (job is None or job["execution_lane"] != "formal"
                or job["job_type"] != "manual_correctness" or job["task_id"] != intent.task_id
                or job["payload"].get("intent_id") != str(jobs.intent_id)
                or job["payload"].get("worker_id") != jobs.worker_id
                or job["payload"].get("claim_token") != str(jobs.claim_token)
                or job["claimed_by"] != self.owner["executor_id"]
                or job["claim_token"] != self.owner["token"]
                or job["lease_id"] != self.owner["lease_id"]
                or job["fencing_token"] != self.owner["fencing_token"]):
            raise Conflict("Formal correctness journal owner differs")
        rows = conn.execute(
            "SELECT event_type, details, created_at FROM job_events WHERE job_id = %s "
            "AND event_type IN ('formal_correctness_invoking', 'formal_correctness_result', "
            "'formal_correctness_unknown', 'formal_correctness_released') ORDER BY created_at",
            (self.job_id,),
        ).fetchall()
        if any(row["details"].get("input_hash") != input_hash for row in rows):
            raise Conflict("Formal correctness invocation already binds another input")
        events = {row["event_type"]: row for row in rows}
        if len(events) != len(rows):
            raise Conflict("Formal correctness journal has duplicate audit facts")
        return job, events

    def begin(self, input_hash: str) -> tuple[dict, bool]:
        repo = self.lease.jobs.claims.dispatcher.repository
        # Exact terminal replay does not reacquire expired hardware authority.
        with repo.connection() as conn:
            _, events = self._locked(conn, input_hash)
            if events:
                return events, False
        self.lease.assert_live(self.job_id, **self.owner)
        prepared = self.lease._prepared()
        with repo.connection() as conn:
            self.lease._lock_authority(conn, prepared)
            conn.execute("SELECT 1 FROM search_rounds WHERE round_id = %s FOR SHARE",
                         (prepared.intent.round_id,)).fetchone()
            budget = conn.execute(
                "SELECT * FROM round_budget_reservations WHERE job_id = %s AND attempt = 1 "
                "FOR SHARE", (self.job_id,),
            ).fetchone()
            if budget is None or budget["state"] != "reserved":
                raise Conflict("Formal correctness invocation budget is not reserved")
            job, events = self._locked(conn, input_hash)
            if events:
                return events, False
            if job["state"] != "running":
                raise Conflict("Formal correctness invocation requires running Job")
            # Recheck physical lease identity and expiry in the insertion transaction.
            resource = conn.execute(
                "SELECT *, expires_at > clock_timestamp() AS live FROM resources "
                "WHERE resource_id = %s FOR UPDATE", (job["resource_id"],),
            ).fetchone()
            if (resource is None or not resource["live"] or resource["state"] != "active"
                    or resource["owner_job_id"] != self.job_id
                    or resource["lease_id"] != self.owner["lease_id"]
                    or resource["fencing_token"] != self.owner["fencing_token"]):
                raise Conflict("Formal correctness invocation lease is stale")
            _insert(conn, "job_events", {
                "job_id": self.job_id, "event_type": "formal_correctness_invoking",
                "details": {"input_hash": input_hash},
            })
            return self._locked(conn, input_hash)[1], True

    def record_result(self, input_hash: str, result: ManualCorrectnessResult,
                      *, wall_seconds: float) -> None:
        result = ManualCorrectnessResult.model_validate(result.model_dump(mode="json"))
        if (isinstance(wall_seconds, bool) or not math.isfinite(wall_seconds) or wall_seconds < 0):
            raise ValueError("Formal correctness usage must be finite and nonnegative")
        details = {"input_hash": input_hash, "result": result.model_dump(mode="json"),
                   "wall_seconds": wall_seconds}
        with self.lease.jobs.claims.dispatcher.repository.connection() as conn:
            job, events = self._locked(conn, input_hash)
            fence, health = result.cleanup_evidence["fence"], result.cleanup_evidence["health"]
            if (str(result.candidate_id) != job["payload"]["candidate_id"]
                    or any(p.profile != job["adapter_profile"] for p in result.adapter_provenance)
                    or fence.get("resource_id") != job["resource_id"]
                    or health.get("resource_id") != job["resource_id"]
                    or fence.get("fencing_token") != job["fencing_token"]):
                raise Conflict("Formal correctness result or cleanup binding differs")
            previous = events.get("formal_correctness_result")
            if previous is not None:
                if previous["details"] != details:
                    raise Conflict("Formal correctness result is immutable")
                return
            if ("formal_correctness_invoking" not in events
                    or "formal_correctness_unknown" in events or job["state"] != "running"):
                raise Conflict("Formal correctness result requires an invoking attempt")
            _insert(conn, "job_events", {
                "job_id": self.job_id, "event_type": "formal_correctness_result",
                "details": details,
            })

    def mark_unknown(self, input_hash: str) -> None:
        with self.lease.jobs.claims.dispatcher.repository.connection() as conn:
            _, events = self._locked(conn, input_hash)
            if "formal_correctness_invoking" not in events:
                raise Conflict("Formal correctness was not invoked")
            if "formal_correctness_result" in events or "formal_correctness_unknown" in events:
                return
            _insert(conn, "job_events", {
                "job_id": self.job_id, "event_type": "formal_correctness_unknown",
                "details": {"input_hash": input_hash},
            })

    def finalize_recorded_result(self, input_hash: str) -> ManualCorrectnessResult:
        """Release from bound cleanup, retain actual lease usage, then settle and finish.

        No Round/member verdict is published here. An incorrect/invalid evaluation
        is still a completed execution. Unknown executions never enter this path.
        """
        repo = self.lease.jobs.claims.dispatcher.repository
        with repo.connection() as conn:
            job, events = self._locked(conn, input_hash)
            recorded = events.get("formal_correctness_result")
            if recorded is None:
                raise Conflict("Formal correctness has no recorded result; recovery required")
            details = recorded["details"]
            result = ManualCorrectnessResult.model_validate(details["result"])
            budget = conn.execute(
                "SELECT * FROM round_budget_reservations WHERE job_id = %s AND attempt = 1",
                (self.job_id,),
            ).fetchone()
            if budget is None:
                raise Conflict("Formal correctness recorded budget is missing")
            released = events.get("formal_correctness_released")
            if released is None:
                resource = conn.execute(
                    "SELECT * FROM resources WHERE resource_id = %s FOR UPDATE",
                    (job["resource_id"],),
                ).fetchone()
                if (resource is None or resource["state"] != "active"
                        or resource["owner_job_id"] != self.job_id
                        or resource["lease_id"] != self.owner["lease_id"]
                        or resource["fencing_token"] != self.owner["fencing_token"]):
                    raise Conflict(
                        "Formal correctness cleanup ownership changed; recovery required"
                    )
                now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
                held = (now - job["claimed_at"]).total_seconds()
                repo._release_resource(
                    conn, job, "formal_correctness_completed", result.cleanup_evidence,
                )
                _insert(conn, "job_events", {
                    "job_id": self.job_id, "event_type": "formal_correctness_released",
                    "details": {"input_hash": input_hash, "lease_held_seconds": held},
                })
                released = self._locked(conn, input_hash)[1]["formal_correctness_released"]
            held = released["details"]["lease_held_seconds"]
            reservation_id = budget["reservation_id"]
            entry = RoundBudgetLedgerEntry(
                ledger_entry_id=uuid5(reservation_id, "formal-correctness-settlement-v1"),
                reservation_id=reservation_id, round_id=budget["round_id"], entry_type="settle",
                reserved=BudgetUsage.model_validate(budget["planned"]),
                actual=BudgetUsage(correctness_attempts=1, wall_seconds=details["wall_seconds"],
                                   exclusive_lease_seconds=held),
                lease_held_seconds=held,
                harness_active_seconds=0,
                raw_usage_evidence_hash="sha256:" + hashlib.sha256(canonical_json_bytes({
                    "job_id": str(self.job_id), "reservation_id": str(reservation_id),
                    "recorded": details, "released": released["details"],
                })).hexdigest(),
                idempotency_key=f"formal-correctness-settle:{self.job_id}",
                created_at=released["created_at"],
            )
        repo.finalize_round_budget(entry)
        with repo.connection() as conn:
            job, events = self._locked(conn, input_hash)
            if job["state"] == "succeeded" and job["result"] == details:
                return result
            if job["state"] != "running":
                raise Conflict("Formal correctness completion requires its running Job")
            conn.execute(
                "UPDATE jobs SET state = 'succeeded', result = %s, "
                "finished_at = clock_timestamp(), "
                "updated_at = clock_timestamp() WHERE job_id = %s", (Jsonb(details), self.job_id),
            )
            _insert(conn, "job_events", {
                "job_id": self.job_id, "event_type": "formal_correctness_completed",
                "details": {"input_hash": input_hash, "verdict": result.verdict},
            })
        return result
