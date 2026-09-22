# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Batch correctness-to-search handoff; no measurement or human signoff."""

import hashlib

from hcuopt.adapters.m1_verification import _correctness_context, _load_hotspot_spec
from hcuopt.contracts.m2 import RoundCandidate
from hcuopt.contracts.v1 import ManualCorrectnessResult
from hcuopt.domain.errors import Conflict
from hcuopt.evaluation.formal_correctness import verify_formal_correctness
from hcuopt.evaluation.m1_verifier import M1CorrectnessEvidenceReference
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.storage.formal_correctness_materials import PostgresFormalCorrectnessMaterialReader
from hcuopt.storage.formal_dispatch import _insert


def reverify_result(adapter, round_, members, payload, result):
    """Use D's raw-file verifier again, without invoking the producer."""
    if result.synthetic or result.candidate_id != _correctness_context(
        payload, payload["_job_context"],
    ).candidate_id:
        raise Conflict("Formal handoff result is synthetic or belongs to another candidate")
    verified = verify_formal_correctness(
        round_authority=round_, members=members,
        context=_correctness_context(payload, payload["_job_context"]),
        hotspot=_load_hotspot_spec(payload, adapter.reader),
        reference=M1CorrectnessEvidenceReference(
            uri=result.raw_evidence_uri, sha256=result.raw_evidence_hash,
        ), verifier=adapter.verifier,
    )
    recorded = adapter.reader.read(
        result.verification_artifact_uri, result.verification_artifact_hash,
    )
    if (canonical_json_bytes(recorded) != canonical_json_bytes(verified)
            or verified.verdict != result.verdict
            or verified.protocol_version != result.protocol_version):
        raise Conflict("Formal handoff verification changed from the recorded result")
    if verified.verdict == "invalid":
        raise Conflict("Formal handoff has invalid evidence; repair or review required")
    return verified


class FormalCorrectnessHandoff:
    def __init__(self, driver):
        self.driver = driver

    def execute(self, *, entries):
        """entries are (original journal, D adapter) for every built member.

        This is a deployment-only operation. It requires a current control-plane
        claim but never reacquires an expired hardware lease. All evidence is
        checked before one atomic family transition. Invalid/unknown evidence
        leaves the family unchanged; numerical failures remain failed members.
        """
        driver = self.driver
        prepared = driver.lease._prepared()
        repo = driver.runtime.repository
        if not entries:
            raise Conflict("Formal handoff requires completed correctness entries")
        for journal, adapter in entries:
            if journal.lease is not driver.lease:
                raise Conflict("Formal handoff journal belongs to another driver")
            driver._validate_adapter(adapter)
        with repo.connection() as conn:
            driver.lease._lock_authority(conn, prepared)
            previous = conn.execute(
                "SELECT details FROM task_events WHERE task_id = %s "
                "AND event_type = 'formal_correctness_family_handoff'",
                (prepared.intent.task_id,),
            ).fetchall()
            if previous:
                if len(previous) != 1:
                    raise Conflict("Formal handoff has duplicate audit records")
                report = previous[0]["details"]
                job_ids = [str(journal.job_id) for journal, _ in entries]
                recorded_ids = [item["job_id"] for item in report["members"].values()]
                digest = "sha256:" + hashlib.sha256(
                    canonical_json_bytes(report["members"]),
                ).hexdigest()
                if (report["round_id"] != str(prepared.intent.round_id)
                        or sorted(job_ids) != sorted(recorded_ids)
                        or report["evidence_hash"] != digest):
                    raise Conflict("Formal handoff replay differs from published family")
                by_job = {item["job_id"]: item for item in report["members"].values()}
                for journal, _ in entries:
                    binding = by_job[str(journal.job_id)]
                    job, events = journal._locked(conn, binding["input_hash"])
                    details = events.get("formal_correctness_result", {}).get("details")
                    if details is None:
                        raise Conflict("Formal handoff replay lost its recorded result")
                    self._require_completed(conn, journal, job, events, details)
                    result = details["result"]
                    if any(result[name] != binding[name] for name in (
                        "verdict", "raw_evidence_hash", "verification_artifact_hash",
                    )):
                        raise Conflict("Formal handoff replay result differs")
                return report
        snapshots = []
        seen = set()
        family = None
        for journal, adapter in entries:
            if journal.lease is not driver.lease:
                raise Conflict("Formal handoff journal belongs to another driver")
            driver._validate_adapter(adapter)
            round_, members, payload = PostgresFormalCorrectnessMaterialReader(journal).load()
            current_family = canonical_json_bytes({
                "round": round_.model_dump(mode="json"),
                "members": [m.model_dump(mode="json") for m in members],
            })
            if family is not None and family != current_family:
                raise Conflict("Formal handoff family changed during verification")
            family = current_family
            with repo.connection() as conn:
                events = conn.execute(
                    "SELECT details FROM job_events WHERE job_id = %s "
                    "AND event_type = 'formal_correctness_result'", (journal.job_id,),
                ).fetchall()
                if len(events) != 1:
                    raise Conflict("Formal handoff requires one durable correctness result")
                details = events[0]["details"]
                job, audit = journal._locked(conn, details["input_hash"])
                self._require_completed(conn, journal, job, audit, details)
            result = ManualCorrectnessResult.model_validate(details["result"])
            if result.candidate_id in seen:
                raise Conflict("Formal handoff repeats a candidate")
            seen.add(result.candidate_id)
            verified = reverify_result(adapter, round_, members, payload, result)
            snapshots.append((journal, details, result, verified, canonical_json_bytes(payload)))
        required = {m.candidate_id for m in members if m.state == "built"}
        if seen != required or len(members) != round_.declared_candidate_count:
            raise Conflict("Formal handoff must cover the complete built family")
        if round_.round_id != prepared.intent.round_id:
            raise Conflict("Formal handoff Round differs from current Intent")
        evidence = {str(result.candidate_id): {
            "job_id": str(journal.job_id), "input_hash": details["input_hash"],
            "verification_artifact_hash": result.verification_artifact_hash,
            "raw_evidence_hash": result.raw_evidence_hash, "verdict": verified.verdict,
        } for journal, details, result, verified, _ in snapshots}
        digest = "sha256:" + hashlib.sha256(canonical_json_bytes(evidence)).hexdigest()
        with repo.connection() as conn:
            driver.lease._lock_authority(conn, prepared)
            row = conn.execute("SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                               (round_.round_id,)).fetchone()
            rows = conn.execute(
                "SELECT * FROM round_candidates WHERE round_id = %s ORDER BY ordinal FOR UPDATE",
                (round_.round_id,),
            ).fetchall()
            current_members = [RoundCandidate.model_validate(
                {k: r[k] for k in RoundCandidate.model_fields},
            ) for r in rows]
            if row is None or canonical_json_bytes({
                "round": repo._search_round_authority(row).model_dump(mode="json"),
                "members": [m.model_dump(mode="json") for m in current_members],
            }) != family:
                raise Conflict("Formal handoff family changed before publication")
            # Re-read every verified payload under the publication transaction
            # before updating any member; partial state would invalidate the
            # frozen build family used by the next member's material reader.
            for journal, details, _result, _verified, payload_bytes in snapshots:
                job, audit = journal._locked(conn, details["input_hash"])
                self._require_completed(conn, journal, job, audit, details)
                _, _, current_payload = PostgresFormalCorrectnessMaterialReader(journal).load(
                    connection=conn,
                )
                if canonical_json_bytes(current_payload) != payload_bytes:
                    raise Conflict("Formal handoff evidence bindings changed before publication")
            for _journal, _details, result, verified, _ in snapshots:
                state = ("correctness_passed" if verified.verdict == "correct"
                         else "correctness_failed")
                conn.execute(
                    "UPDATE round_candidates SET state = %s, updated_at = clock_timestamp() "
                    "WHERE round_id = %s AND candidate_id = %s AND state = 'built'",
                    (state, round_.round_id, result.candidate_id),
                )
            conn.execute(
                "UPDATE search_rounds SET state = 'search_measuring', version = version + 1, "
                "updated_at = clock_timestamp() WHERE round_id = %s", (round_.round_id,),
            )
            report = {"round_id": str(round_.round_id), "evidence_hash": digest,
                      "members": evidence, "hcu_started": False,
                      "automatic_release_allowed": False}
            _insert(conn, "task_events", {
                "task_id": round_.task_id, "event_type": "formal_correctness_family_handoff",
                "details": report,
            })
        return report

    @staticmethod
    def _require_completed(conn, journal, job, events, details):
        budget = conn.execute(
            "SELECT state FROM round_budget_reservations WHERE job_id = %s AND attempt = 1",
            (journal.job_id,),
        ).fetchone()
        health = details["result"].get("cleanup_evidence", {}).get("health", {})
        if (job["state"] != "succeeded" or job["result"] != details
                or "formal_correctness_released" not in events
                or "formal_correctness_unknown" in events
                or events.get("formal_correctness_result", {}).get("details") != details
                or budget is None or budget["state"] != "settled"
                or health.get("healthy") is not True or health.get("quarantined") is not False):
            raise Conflict("Formal handoff requires completed, released, settled healthy results")
