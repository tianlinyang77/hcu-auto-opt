# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Trusted deployment inspection and known-result reconciliation, never re-execution."""

import hashlib

from hcuopt.domain.errors import Conflict
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.storage.formal_correctness_journal import PostgresFormalCorrectnessJournal
from hcuopt.storage.formal_dispatch import _insert


class PostgresFormalCorrectnessRecovery:
    def __init__(self, journal: PostgresFormalCorrectnessJournal):
        self.journal = journal
        self.repository = journal.lease.jobs.claims.dispatcher.repository

    def inspect(self, input_hash: str) -> dict:
        """Credential-free snapshot; no process inspection or cleanup is implied."""
        with self.repository.connection() as conn:
            return self._inspect(conn, input_hash)

    def _inspect(self, conn, input_hash):
        journal = self.journal
        job, events = journal._locked(conn, input_hash)
        resource = conn.execute(
            "SELECT * FROM resources WHERE resource_id = %s FOR SHARE",
            (job["resource_id"],),
        ).fetchone()
        budget = conn.execute(
            "SELECT reservation_id, state FROM round_budget_reservations "
            "WHERE job_id = %s AND attempt = 1 FOR SHARE", (journal.job_id,),
        ).fetchone()
        recorded = events.get("formal_correctness_result")
        released = events.get("formal_correctness_released")
        owns_resource = bool(
            resource and resource["state"] == "active"
            and resource["owner_job_id"] == journal.job_id
            and resource["lease_id"] == journal.owner["lease_id"]
            and resource["fencing_token"] == journal.owner["fencing_token"]
        )
        if budget is None:
            status = "inconsistent"
        elif recorded is not None:
            if job["state"] == "succeeded":
                status = ("completed" if released and budget["state"] == "settled"
                          and job["result"] == recorded["details"] else "inconsistent")
            elif job["state"] != "running":
                status = "inconsistent"
            elif released:
                status = ("settlement_pending" if budget["state"] in {"reserved", "settled"}
                          else "inconsistent")
            elif owns_resource and budget["state"] == "reserved":
                status = "result_ready"
            else:
                status = "ownership_or_budget_conflict"
        elif "formal_correctness_unknown" in events:
            status = "unknown_requires_manual_recovery"
        elif "formal_correctness_invoking" in events:
            status = "invocation_unresolved"
        else:
            status = "not_invoked"
        report = {
            "schema_version": "formal-correctness-recovery-v1",
            "job_id": str(journal.job_id), "input_hash": input_hash,
            "job_state": job["state"], "status": status,
            "resource_id": job["resource_id"],
            "resource_state": resource["state"] if resource else "missing",
            "resource_owned_by_attempt": owns_resource,
            "budget_state": budget["state"] if budget else "missing",
            "reservation_id": str(budget["reservation_id"]) if budget else None,
            "recorded_result": recorded is not None,
            "release_recorded": released is not None,
            "reconciliation_allowed": status in {"result_ready", "settlement_pending", "completed"},
            "execution_retry_allowed": False,
            "automatic_release_allowed": False,
            "required_manual_checks": [] if status in {
                "result_ready", "settlement_pending", "completed",
            } else [
                "fence_original_executor_before_any_resource_reuse",
                "inspect_exact_job_owned_containers_and_processes",
                "retain_fresh_cleanup_and_resource_health_evidence",
                "reconcile_unknown_usage_without_inventing_zero_cost",
            ],
        }
        report["snapshot_hash"] = "sha256:" + hashlib.sha256(
            canonical_json_bytes(report),
        ).hexdigest()
        return report

    def reconcile_known_result(
        self, input_hash: str, *, expected_snapshot_hash: str, requested_by: str,
        request_id: str,
    ):
        """Audit an operator request, then reuse the original idempotent finalizer.

        Trusted deployment only, not a public verdict/cleanup submission endpoint.
        Unknown attempts have no release path here. A replay after partial success
        keeps the original request identity and does not demand a fresh preview.
        """
        if any(not isinstance(v, str) or not v.strip() or len(v) > 256
               for v in (requested_by, request_id, expected_snapshot_hash)):
            raise ValueError("recovery requires bounded actor, request ID and snapshot Hash")
        details = {
            "input_hash": input_hash, "snapshot_hash": expected_snapshot_hash,
            "requested_by": requested_by, "request_id": request_id,
        }
        with self.repository.connection() as conn:
            report = self._inspect(conn, input_hash)
            previous = conn.execute(
                "SELECT details FROM job_events WHERE job_id = %s AND event_type = "
                "'formal_correctness_reconciliation_requested' AND details->>'request_id' = %s",
                (self.journal.job_id, request_id),
            ).fetchall()
            if previous:
                if len(previous) != 1 or previous[0]["details"] != details:
                    raise Conflict("Formal correctness recovery request is immutable")
            elif report["snapshot_hash"] != expected_snapshot_hash:
                raise Conflict("Formal correctness recovery snapshot changed; inspect again")
            if not report["reconciliation_allowed"]:
                raise Conflict(
                    "Formal correctness recovery requires a recorded result and ownership",
                )
            if not previous:
                _insert(conn, "job_events", {
                    "job_id": self.journal.job_id,
                    "event_type": "formal_correctness_reconciliation_requested", "details": details,
                })
        return self.journal.finalize_recorded_result(input_hash)
