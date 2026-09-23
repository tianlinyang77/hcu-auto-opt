# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Read the immutable inputs for D's Formal Search barrier.

This module deliberately reconstructs the batch from the deployment database
and B/D receipts.  It is not an execution worker: it neither reserves a device
nor changes candidate state.  A missing or ambiguous terminal fact is an error
rather than a reason to retry or invent a failure result.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from uuid import UUID

from hcuopt.contracts.m2 import RoundCandidate, SearchRound
from hcuopt.contracts.m2_formal_authority_v1 import formal_authority_context_ref
from hcuopt.contracts.m2_formal_execution_v1 import (
    M2FormalPhaseExecutionReceiptRef,
    M2FormalPhaseExecutionRequest,
)
from hcuopt.domain.enums import RoundCandidateState, RoundPhase
from hcuopt.domain.errors import Conflict
from hcuopt.evaluation.m2_models import BarrierMemberResult
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.source_hash import file_uri_to_path
from hcuopt.storage.formal_phase_journal import PostgresFormalPhaseJournal
from hcuopt.workers.formal_search_consumer import FormalSearchBatchMaterials


@dataclass(frozen=True)
class _CorrectnessFact:
    verdict: str
    raw_evidence_hash: str
    verification_artifact_hash: str


class PostgresFormalSearchBatchMaterialReader:
    """Build one complete Search family from durable, independently written facts."""

    def __init__(self, journal: PostgresFormalPhaseJournal) -> None:
        self.journal = journal

    def load(self) -> FormalSearchBatchMaterials:
        journal = self.journal
        claims = journal.claims
        claims.assert_active(journal.intent_id, journal.worker_id, journal.claim_token)
        repository = claims.dispatcher.repository
        with repository.connection() as connection:
            intent = claims._lock_deployment_intent(connection, journal.intent_id)
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s AND task_id = %s FOR SHARE",
                (intent.round_id, intent.task_id),
            ).fetchone()
            context_row = connection.execute(
                "SELECT * FROM formal_round_authority_contexts "
                "WHERE round_id = %s AND task_id = %s FOR SHARE",
                (intent.round_id, intent.task_id),
            ).fetchone()
            candidate_rows = connection.execute(
                "SELECT * FROM round_candidates WHERE round_id = %s ORDER BY ordinal FOR SHARE",
                (intent.round_id,),
            ).fetchall()
            if round_row is None or context_row is None:
                raise Conflict("Formal Search requires a durable Round and Authority Context")
            round_authority = repository._search_round_authority(round_row)
            if (
                round_authority.round_id != intent.round_id
                or round_authority.task_id != intent.task_id
                or round_authority.candidate_family_hash is None
                or round_authority.artifact_family_hash is None
            ):
                raise Conflict("Formal Search Round authority is incomplete or changed")
            context = formal_authority_context_ref(
                repository._formal_authority_context(context_row)
            )
            if (
                context.round_id != round_authority.round_id
                or context.task_id != round_authority.task_id
                or context.candidate_family_hash != round_authority.candidate_family_hash
                or context.artifact_family_hash != round_authority.artifact_family_hash
            ):
                raise Conflict("Formal Search Authority Context differs from its Round")
            members = tuple(
                RoundCandidate.model_validate(
                    {name: row[name] for name in RoundCandidate.model_fields}
                )
                for row in candidate_rows
            )
            expected = {
                (item.candidate_id, item.round_candidate_id)
                for item in intent.candidate_bindings
            }
            actual = {(item.candidate_id, item.round_candidate_id) for item in members}
            if (
                len(members) != round_authority.declared_candidate_count
                or actual != expected
                or len(actual) != len(members)
            ):
                raise Conflict("Formal Search family differs from the claimed Intent")
            correctness = self._correctness_facts(connection, round_authority)
            phase_rows = connection.execute(
                "SELECT * FROM formal_phase_journal WHERE intent_id = %s AND phase = 'search' "
                "ORDER BY candidate_id FOR SHARE",
                (journal.intent_id,),
            ).fetchall()
            phases = {row["candidate_id"]: row for row in phase_rows}
            if len(phases) != len(phase_rows) or not set(phases).issubset(
                {member.candidate_id for member in members}
            ):
                raise Conflict("Formal Search phase journal has duplicate or foreign members")
            result_members: list[BarrierMemberResult] = []
            references = []
            for member in members:
                fact = correctness.get(member.candidate_id)
                if fact is None:
                    raise Conflict("Formal Search requires a completed correctness handoff")
                if fact.verdict != "correct":
                    result_members.append(self._correctness_failed(member, fact))
                    if member.candidate_id in phases:
                        raise Conflict(
                            "Incorrect Formal candidate must not have a Search measurement"
                        )
                    continue
                phase = phases.get(member.candidate_id)
                if (
                    phase is None
                    or phase["state"] != "receipt_recorded"
                    or phase["receipt_ref"] is None
                ):
                    raise Conflict(
                        "Formal Search requires one terminal Search receipt per correct member"
                    )
                receipt_ref = M2FormalPhaseExecutionReceiptRef.model_validate(phase["receipt_ref"])
                request = M2FormalPhaseExecutionRequest.model_validate(phase["request"])
                receipt = journal.receipt_store.load_for_request(receipt_ref, request)
                record = receipt.execution
                if (
                    phase["execution_id"] != record.execution_id
                    or phase["candidate_id"] != member.candidate_id
                    or request.binding.round_id != round_authority.round_id
                    or request.binding.candidate_id != member.candidate_id
                    or request.binding.round_candidate_id != member.round_candidate_id
                    or request.binding.phase is not RoundPhase.SEARCH
                    or request.binding.task_id != round_authority.task_id
                    or request.binding.candidate_family_hash
                    != round_authority.candidate_family_hash
                    or request.binding.artifact_family_hash
                    != round_authority.artifact_family_hash
                    or request.binding.phase_plan_hash != round_authority.search_plan_hash
                    or request.binding.authority_context_hash != context.context_hash
                    or request.binding.artifact_id != member.artifact_id
                    or request.binding.artifact_hash != member.artifact_hash
                    or record.binding != request.binding
                    or record.usage_evidence_hash is None
                    or record.cleanup_evidence_hash is None
                ):
                    raise Conflict("Formal Search receipt differs from durable phase bindings")
                usage = _read_hashed_json(
                    record.usage_evidence_uri, record.usage_evidence_hash
                )
                cleanup = _read_hashed_json(
                    record.cleanup_evidence_uri, record.cleanup_evidence_hash
                )
                _verify_phase_evidence_payloads(request, record, usage, cleanup)
                reservation_id = request.reservation.reservation_id
                budget_rows = connection.execute(
                    "SELECT * FROM round_budget_ledger WHERE reservation_id = %s "
                    "AND entry_type = 'settle' FOR SHARE",
                    (reservation_id,),
                ).fetchall()
                reservation = connection.execute(
                    "SELECT * FROM round_budget_reservations "
                    "WHERE reservation_id = %s FOR SHARE",
                    (reservation_id,),
                ).fetchone()
                if (
                    len(budget_rows) != 1
                    or reservation is None
                    or reservation["state"] != "settled"
                    or reservation["round_id"] != round_authority.round_id
                    or reservation["candidate_id"] != member.candidate_id
                    or reservation["phase"] != RoundPhase.SEARCH.value
                    or budget_rows[0]["raw_usage_evidence_hash"]
                    != record.usage_evidence_hash
                    or budget_rows[0]["actual"] != usage["actual"]
                    or budget_rows[0]["lease_held_seconds"]
                    != record.lease_held_seconds
                    or budget_rows[0]["harness_active_seconds"]
                    != record.harness_active_seconds
                ):
                    raise Conflict("Formal Search budget evidence differs from settled ledger")
                if record.status == "succeeded":
                    reference = record.measurement_ref
                    if reference is None:
                        raise Conflict(
                            "Successful Formal Search receipt lacks a measurement reference"
                        )
                    result_members.append(
                        BarrierMemberResult(
                            round_candidate_id=member.round_candidate_id,
                            candidate_id=member.candidate_id,
                            candidate_state=RoundCandidateState.SEARCH_MEASURED,
                            artifact_id=member.artifact_id,
                            artifact_hash=member.artifact_hash,
                            correctness_evidence_hash=fact.verification_artifact_hash,
                            round_measurement_ref_id=reference.round_measurement_ref_id,
                            budget_usage_evidence_hash=record.usage_evidence_hash,
                            cleanup_evidence_hash=record.cleanup_evidence_hash,
                            synthetic=False,
                        )
                    )
                    references.append(reference)
                else:
                    result_members.append(
                        BarrierMemberResult(
                            round_candidate_id=member.round_candidate_id,
                            candidate_id=member.candidate_id,
                            candidate_state=RoundCandidateState.SEARCH_FAILED,
                            artifact_id=member.artifact_id,
                            artifact_hash=member.artifact_hash,
                            correctness_evidence_hash=fact.verification_artifact_hash,
                            failure_evidence_hash=receipt_ref.content_hash,
                            budget_usage_evidence_hash=record.usage_evidence_hash,
                            cleanup_evidence_hash=record.cleanup_evidence_hash,
                            synthetic=False,
                        )
                    )
        return FormalSearchBatchMaterials(
            round_authority=round_authority,
            context=context,
            members=tuple(result_members),
            references=tuple(references),
        )

    @staticmethod
    def _correctness_failed(member: RoundCandidate, fact: _CorrectnessFact) -> BarrierMemberResult:
        if member.artifact_id is None or member.artifact_hash is None:
            raise Conflict("Formal correctness failure lacks its built Artifact")
        return BarrierMemberResult(
            round_candidate_id=member.round_candidate_id,
            candidate_id=member.candidate_id,
            candidate_state=RoundCandidateState.CORRECTNESS_FAILED,
            artifact_id=member.artifact_id,
            artifact_hash=member.artifact_hash,
            failure_evidence_hash=fact.verification_artifact_hash,
            budget_usage_evidence_hash=fact.raw_evidence_hash,
            synthetic=False,
        )

    @staticmethod
    def _correctness_facts(
        connection, round_authority: SearchRound
    ) -> dict[UUID, _CorrectnessFact]:
        rows = connection.execute(
            "SELECT details FROM task_events WHERE task_id = %s "
            "AND event_type = 'formal_correctness_family_handoff' "
            "AND details->>'round_id' = %s FOR SHARE",
            (round_authority.task_id, str(round_authority.round_id)),
        ).fetchall()
        if len(rows) != 1:
            raise Conflict("Formal Search requires exactly one correctness family handoff")
        report = rows[0]["details"]
        raw_members = report.get("members") if isinstance(report, dict) else None
        if not isinstance(raw_members, dict):
            raise Conflict("Formal correctness handoff is malformed")
        facts: dict[UUID, _CorrectnessFact] = {}
        for candidate_text, item in raw_members.items():
            try:
                candidate_id = UUID(candidate_text)
            except (TypeError, ValueError) as error:
                raise Conflict("Formal correctness handoff candidate is invalid") from error
            if not isinstance(item, dict):
                raise Conflict("Formal correctness handoff member is malformed")
            try:
                fact = _CorrectnessFact(
                    verdict=item["verdict"],
                    raw_evidence_hash=item["raw_evidence_hash"],
                    verification_artifact_hash=item["verification_artifact_hash"],
                )
            except KeyError as error:
                raise Conflict("Formal correctness handoff lacks immutable evidence") from error
            if candidate_id in facts:
                raise Conflict("Formal correctness handoff repeats a candidate")
            facts[candidate_id] = fact
        return facts


__all__ = ["PostgresFormalSearchBatchMaterialReader"]


def _read_hashed_json(uri: str | None, expected_hash: str) -> dict:
    if not uri:
        raise Conflict("Formal phase receipt has no evidence URI")
    path = file_uri_to_path(uri)
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= 2 * 1024 * 1024
        ):
            raise Conflict("Formal phase evidence is not a bounded regular file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise Conflict("Formal phase evidence changed during open")
            encoded = stream.read(2 * 1024 * 1024 + 1)
            after = os.fstat(stream.fileno())
        if (
            len(encoded) > 2 * 1024 * 1024
            or (after.st_size, after.st_mtime_ns)
            != (before.st_size, before.st_mtime_ns)
            or "sha256:" + hashlib.sha256(encoded).hexdigest() != expected_hash
        ):
            raise Conflict("Formal phase evidence Hash or identity changed")
        value = json.loads(encoded)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        if isinstance(error, Conflict):
            raise
        raise Conflict("Formal phase evidence cannot be read") from error
    if not isinstance(value, dict) or canonical_json_bytes(value) != encoded:
        raise Conflict("Formal phase evidence is not a canonical JSON object")
    return value


def _verify_phase_evidence_payloads(request, record, usage: dict, cleanup: dict) -> None:
    binding = request.binding
    if (
        usage.get("schema_version") != "m2a-formal-phase-budget-usage-v1"
        or usage.get("round_id") != str(binding.round_id)
        or usage.get("candidate_id") != str(binding.candidate_id)
        or usage.get("phase") != binding.phase.value
        or usage.get("reservation_id") != str(request.reservation.reservation_id)
        or usage.get("status") != record.status
        or usage.get("planned") != request.reservation.planned.model_dump(mode="json")
        or usage.get("actual") != record.actual.model_dump(mode="json")
        or usage.get("lease_held_seconds") != record.lease_held_seconds
        or usage.get("harness_active_seconds") != record.harness_active_seconds
        or usage.get("error_code") != record.error_code
        or usage.get("synthetic") is not False
        or usage.get("producer_verdict") is not None
        or usage.get("performance_conclusion") != "not_measured"
    ):
        raise Conflict("Formal phase budget evidence differs from its execution receipt")
    if (
        cleanup.get("schema_version") != "m2a-formal-phase-cleanup-v1"
        or cleanup.get("binding") != binding.model_dump(mode="json")
        or cleanup.get("status") != record.status
        or cleanup.get("cleanup_status") != record.cleanup_status
        or cleanup.get("cleanup_evidence") != record.cleanup_evidence
        or cleanup.get("synthetic") is not False
        or cleanup.get("automatic_release_allowed") is not False
    ):
        raise Conflict("Formal phase cleanup evidence differs from its execution receipt")
