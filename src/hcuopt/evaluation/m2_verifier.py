# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, ValidationError, field_validator, model_validator

from hcuopt.contracts.m2 import SearchRound
from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.domain.enums import (
    RoundBarrierOutcome,
    RoundPhase,
    RoundTerminalReason,
    SearchRoundRunMode,
    SearchRoundState,
)
from hcuopt.evaluation.m2_models import (
    BarrierMemberResult,
    FrozenEvaluationModel,
    MultipleComparisonResult,
    RoundBarrierResult,
    RoundCandidateEvidence,
    RoundEvidenceBundle,
)
from hcuopt.evaluation.m2_statistics import (
    M2_FWER_PROTOCOL_VERSION,
    M2_HOLDOUT_BARRIER_RULE_VERSION,
    M2_SEARCH_SELECTION_RULE_VERSION,
    SearchBarrierDecision,
    m2_fwer_protocol_hash,
    recompute_multiple_comparison_result_hash,
)
from hcuopt.measurement.evidence import EvidenceArtifact, canonical_json_bytes

M2_EVIDENCE_INDEX_SCHEMA_VERSION = "m2a-evidence-index-v1"
MAX_EVIDENCE_INDEX_ENTRIES = 256
MAX_EVIDENCE_INDEX_DEPTH = 8


class M2RoundEvidenceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvidenceByteReader(Protocol):
    def read_raw_bytes(self, uri: str, expected_hash: str) -> bytes: ...


class SyntheticEvidenceStore:
    """Content-addressed in-memory evidence for Scripted control-flow runs only."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def publish(self, value: object) -> EvidenceArtifact:
        return self.publish_bytes(canonical_json_bytes(value))

    def publish_bytes(self, encoded: bytes) -> EvidenceArtifact:
        if not encoded:
            raise ValueError("synthetic evidence must not be empty")
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        uri = self._uri(digest)
        existing = self._blobs.setdefault(uri, bytes(encoded))
        if existing != encoded:
            raise ValueError("content-addressed synthetic evidence collision")
        return EvidenceArtifact(uri=uri, sha256=digest, byte_count=len(encoded))

    def artifact_for_hash(self, expected_hash: str) -> EvidenceArtifact:
        uri = self._uri(expected_hash)
        encoded = self._blobs.get(uri)
        if encoded is None:
            raise M2RoundEvidenceError(
                "evidence_unavailable",
                f"synthetic evidence is unavailable for {expected_hash}",
            )
        return EvidenceArtifact(
            uri=uri,
            sha256=expected_hash,
            byte_count=len(encoded),
        )

    def read_raw_bytes(self, uri: str, expected_hash: str) -> bytes:
        if uri != self._uri(expected_hash):
            raise M2RoundEvidenceError(
                "evidence_uri_hash_mismatch",
                "synthetic evidence URI does not match its expected Hash",
            )
        encoded = self._blobs.get(uri)
        if encoded is None:
            raise M2RoundEvidenceError(
                "evidence_unavailable", "synthetic evidence URI is unavailable"
            )
        actual = "sha256:" + hashlib.sha256(encoded).hexdigest()
        if actual != expected_hash:
            raise M2RoundEvidenceError("evidence_hash_mismatch", "synthetic evidence Hash changed")
        return encoded

    @staticmethod
    def _uri(expected_hash: str) -> str:
        if re.fullmatch(r"sha256:[0-9a-f]{64}", expected_hash) is None:
            raise ValueError("synthetic evidence requires a SHA-256 identity")
        return f"scripted-evidence://sha256/{expected_hash.removeprefix('sha256:')}"


class M2EvidenceIndexEntry(FrozenEvaluationModel):
    role: str = Field(pattern=r"^[a-z0-9][a-z0-9_./-]{0,299}$")
    evidence_type: str = Field(min_length=1, max_length=200)
    uri: str = Field(min_length=1, max_length=4000)
    sha256: str = Field(pattern=SHA256_PATTERN)
    producer: str = Field(min_length=1, max_length=300)
    retention_owner: str = Field(min_length=1, max_length=300)
    accessibility_status: Literal["verified"] = "verified"
    accessibility_checked_at: datetime
    children: tuple[M2EvidenceIndexEntry, ...] = Field(default=(), max_length=64)

    @field_validator("children", mode="before")
    @classmethod
    def freeze_children(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_deterministic_children(self) -> M2EvidenceIndexEntry:
        if (
            self.accessibility_checked_at.tzinfo is None
            or self.accessibility_checked_at.utcoffset() is None
        ):
            raise ValueError("Evidence accessibility time must be timezone-aware")
        roles = tuple(item.role for item in self.children)
        if len(set(roles)) != len(roles) or roles != tuple(sorted(roles)):
            raise ValueError("Evidence Index children must be unique and ordered")
        return self


class M2EvidenceIndex(FrozenEvaluationModel):
    schema_version: Literal["m2a-evidence-index-v1"] = M2_EVIDENCE_INDEX_SCHEMA_VERSION
    round_id: UUID
    entries: tuple[M2EvidenceIndexEntry, ...] = Field(min_length=1, max_length=64)
    synthetic: Literal[True] = True
    created_at: datetime

    @field_validator("entries", mode="before")
    @classmethod
    def freeze_entries(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_deterministic_root(self) -> M2EvidenceIndex:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("Evidence Index time must be timezone-aware")
        roles = tuple(item.role for item in self.entries)
        if len(set(roles)) != len(roles) or roles != tuple(sorted(roles)):
            raise ValueError("Evidence Index root entries must be unique and ordered")
        return self


@dataclass(frozen=True, slots=True)
class M2EvidenceRequirement:
    role: str
    evidence_type: str
    sha256: str
    expected_bytes: bytes | None = None


@dataclass(frozen=True, slots=True)
class PublishedM2EvidenceIndex:
    index: M2EvidenceIndex
    artifact: EvidenceArtifact


def scripted_round_evidence_requirements(
    *,
    round_authority: SearchRound,
    search_decision: SearchBarrierDecision,
    budget_ledger_hash: str,
    holdout_barrier: RoundBarrierResult | None = None,
    multiple_comparison: MultipleComparisonResult | None = None,
) -> tuple[M2EvidenceRequirement, ...]:
    _validate_round_path(
        round_authority=round_authority,
        search_decision=search_decision,
        holdout_barrier=holdout_barrier,
        multiple_comparison=multiple_comparison,
    )
    requirements: dict[str, M2EvidenceRequirement] = {}
    _add_requirement(
        requirements,
        role="search/plan",
        evidence_type="search_plan",
        sha256=round_authority.search_plan_hash,
    )
    _add_document(
        requirements,
        role="search/input-summary",
        evidence_type="search_barrier_input",
        value=search_decision.input_summary,
    )
    _add_document(
        requirements,
        role="search/barrier",
        evidence_type="round_barrier",
        value=search_decision.barrier,
    )
    _add_requirement(
        requirements,
        role="budget/ledger",
        evidence_type="round_budget_ledger",
        sha256=budget_ledger_hash,
    )
    for statistics in search_decision.input_summary.statistics:
        prefix = f"candidate/{statistics.candidate_id}/search"
        _add_requirement(
            requirements,
            role=f"{prefix}/raw-fixture",
            evidence_type="scripted_phase_fixture_evidence",
            sha256=statistics.raw_evidence_hash,
        )
        _add_requirement(
            requirements,
            role=f"{prefix}/baseline-samples",
            evidence_type="baseline_sample_set",
            sha256=statistics.baseline_sample_set_hash,
        )
    for member in search_decision.barrier.members:
        _add_member_requirements(requirements, member=member, phase="search")

    if holdout_barrier is not None and multiple_comparison is not None:
        holdout_plan_hash = round_authority.holdout_plan_hash
        holdout_reveal_hash = round_authority.holdout_reveal_evidence_hash
        if holdout_plan_hash is None or holdout_reveal_hash is None:
            raise M2RoundEvidenceError(
                "holdout_evidence_incomplete",
                "Holdout evidence requirements need Plan and Reveal identities",
            )
        _add_requirement(
            requirements,
            role="holdout/plan",
            evidence_type="holdout_plan",
            sha256=holdout_plan_hash,
        )
        _add_requirement(
            requirements,
            role="holdout/reveal",
            evidence_type="holdout_reveal",
            sha256=holdout_reveal_hash,
        )
        _add_document(
            requirements,
            role="holdout/barrier",
            evidence_type="round_barrier",
            value=holdout_barrier,
        )
        _add_document(
            requirements,
            role="holdout/multiple-comparison",
            evidence_type="multiple_comparison",
            value=multiple_comparison,
        )
        for member in holdout_barrier.members:
            _add_member_requirements(requirements, member=member, phase="holdout")
        for result in multiple_comparison.candidate_results:
            prefix = f"candidate/{result.candidate_id}/holdout"
            if result.raw_evidence_hash is not None:
                _add_requirement(
                    requirements,
                    role=f"{prefix}/raw-fixture",
                    evidence_type="scripted_phase_fixture_evidence",
                    sha256=result.raw_evidence_hash,
                )
            if result.baseline_sample_set_hash is not None:
                _add_requirement(
                    requirements,
                    role=f"{prefix}/baseline-samples",
                    evidence_type="baseline_sample_set",
                    sha256=result.baseline_sample_set_hash,
                )
    return tuple(requirements[key] for key in sorted(requirements))


def publish_scripted_evidence_index(
    *,
    store: SyntheticEvidenceStore,
    round_id: UUID,
    requirements: Iterable[M2EvidenceRequirement],
    producer: str,
    retention_owner: str,
    created_at: datetime,
) -> PublishedM2EvidenceIndex:
    entries: list[M2EvidenceIndexEntry] = []
    for requirement in sorted(requirements, key=lambda item: item.role):
        if requirement.expected_bytes is not None:
            artifact = store.publish_bytes(requirement.expected_bytes)
            if artifact.sha256 != requirement.sha256:
                raise M2RoundEvidenceError(
                    "evidence_document_hash_mismatch",
                    f"generated document disagrees with {requirement.role}",
                )
        else:
            artifact = store.artifact_for_hash(requirement.sha256)
        store.read_raw_bytes(artifact.uri, artifact.sha256)
        entries.append(
            M2EvidenceIndexEntry(
                role=requirement.role,
                evidence_type=requirement.evidence_type,
                uri=artifact.uri,
                sha256=artifact.sha256,
                producer=producer,
                retention_owner=retention_owner,
                accessibility_checked_at=created_at,
            )
        )
    index = M2EvidenceIndex(
        round_id=round_id,
        entries=tuple(entries),
        created_at=created_at,
    )
    return PublishedM2EvidenceIndex(index=index, artifact=store.publish(index))


def build_scripted_round_evidence(
    *,
    round_authority: SearchRound,
    search_decision: SearchBarrierDecision,
    budget_ledger_hash: str,
    evidence_index_uri: str,
    evidence_index_hash: str,
    evidence_reader: EvidenceByteReader,
    holdout_barrier: RoundBarrierResult | None = None,
    multiple_comparison: MultipleComparisonResult | None = None,
) -> RoundEvidenceBundle:
    requirements = scripted_round_evidence_requirements(
        round_authority=round_authority,
        search_decision=search_decision,
        budget_ledger_hash=budget_ledger_hash,
        holdout_barrier=holdout_barrier,
        multiple_comparison=multiple_comparison,
    )
    index = _read_and_verify_index(
        reader=evidence_reader,
        uri=evidence_index_uri,
        expected_hash=evidence_index_hash,
        round_id=round_authority.round_id,
        requirements=requirements,
    )
    candidate_evidence = _candidate_evidence(
        search_barrier=search_decision.barrier,
        holdout_barrier=holdout_barrier,
    )
    holdout_completed = holdout_barrier is not None
    terminal_reason = (
        RoundTerminalReason.HOLDOUT_COMPLETED
        if holdout_completed
        else RoundTerminalReason.NO_PROMOTABLE_CANDIDATE
    )
    verdict_counts: dict[str, int] = {}
    recommended = None
    if multiple_comparison is not None:
        for result in multiple_comparison.candidate_results:
            verdict_counts[result.verdict.value] = verdict_counts.get(result.verdict.value, 0) + 1
        recommended = multiple_comparison.recommended_candidate_id
    summary: dict[str, Any] = {
        "performance_conclusion": "not_measured",
        "evidence_authority": "synthetic_fixture_only",
        "terminal_reason": terminal_reason.value,
        "candidate_count": len(candidate_evidence),
        "holdout_member_count": len(holdout_barrier.members) if holdout_barrier is not None else 0,
        "fixture_verdict_counts": verdict_counts,
        "recommended_fixture_candidate_id": str(recommended) if recommended else None,
    }
    values: dict[str, Any] = {
        "round_id": round_authority.round_id,
        "task_id": round_authority.task_id,
        "run_mode": SearchRoundRunMode.SCRIPTED,
        "terminal_reason": terminal_reason,
        "candidate_family_hash": round_authority.candidate_family_hash,
        "artifact_family_hash": round_authority.artifact_family_hash,
        "holdout_family_hash": round_authority.holdout_family_hash,
        "search_plan_hash": round_authority.search_plan_hash,
        "holdout_plan_commitment": round_authority.holdout_plan_commitment,
        "holdout_plan_hash": round_authority.holdout_plan_hash,
        "holdout_reveal_evidence_hash": round_authority.holdout_reveal_evidence_hash,
        "candidate_evidence": candidate_evidence,
        "search_barrier_id": search_decision.barrier.barrier_id,
        "holdout_barrier_id": holdout_barrier.barrier_id if holdout_barrier is not None else None,
        "multiple_comparison_id": multiple_comparison.multiple_comparison_id
        if multiple_comparison is not None
        else None,
        "budget_ledger_hash": budget_ledger_hash,
        "evidence_index_uri": evidence_index_uri,
        "evidence_index_hash": evidence_index_hash,
        "summary": summary,
        "synthetic": True,
        "automatic_release_allowed": False,
    }
    input_digest = _digest(values)
    return RoundEvidenceBundle(
        round_evidence_bundle_id=uuid5(NAMESPACE_URL, f"hcuopt:m2-round-evidence:{input_digest}"),
        created_at=index.created_at,
        **values,
    )


def require_formal_round_signoff(
    *, round_authority: SearchRound, evidence: RoundEvidenceBundle
) -> None:
    if (
        round_authority.run_mode is not SearchRoundRunMode.FORMAL
        or evidence.run_mode is not SearchRoundRunMode.FORMAL
        or evidence.synthetic
    ):
        raise M2RoundEvidenceError(
            "synthetic_round_not_signable",
            "Scripted or synthetic Round Evidence cannot enter Formal Signoff",
        )
    if (
        round_authority.state is not SearchRoundState.AWAITING_SIGNOFF
        or round_authority.round_id != evidence.round_id
        or round_authority.task_id != evidence.task_id
    ):
        raise M2RoundEvidenceError(
            "round_signoff_evidence_mismatch",
            "Formal Signoff Evidence is not the final Bundle for this Round",
        )


def _validate_round_path(
    *,
    round_authority: SearchRound,
    search_decision: SearchBarrierDecision,
    holdout_barrier: RoundBarrierResult | None,
    multiple_comparison: MultipleComparisonResult | None,
) -> None:
    search_barrier = search_decision.barrier
    if (
        round_authority.run_mode is not SearchRoundRunMode.SCRIPTED
        or round_authority.candidate_family_hash is None
        or round_authority.artifact_family_hash is None
        or search_barrier.round_id != round_authority.round_id
        or search_barrier.phase is not RoundPhase.SEARCH
        or search_barrier.input_family_hash != round_authority.artifact_family_hash
        or search_barrier.rule_version != M2_SEARCH_SELECTION_RULE_VERSION
        or search_barrier.rule_hash != round_authority.selection_rule_hash
        or not search_barrier.synthetic
        or search_decision.input_summary.round_id != round_authority.round_id
        or _document_hash(search_decision.input_summary) != search_barrier.input_summary_hash
        or len(search_barrier.members) != round_authority.declared_candidate_count
    ):
        raise M2RoundEvidenceError(
            "round_evidence_binding_mismatch",
            "Search Barrier does not bind the Scripted Round and frozen Families",
        )
    promoted = search_barrier.promoted_candidate_ids
    if not promoted:
        holdout_fields = (
            round_authority.holdout_family_hash,
            round_authority.holdout_plan_hash,
            round_authority.holdout_reveal_evidence_hash,
            holdout_barrier,
            multiple_comparison,
        )
        if (
            round_authority.state is not SearchRoundState.SEARCH_BARRIER
            or search_barrier.outcome is not RoundBarrierOutcome.NO_PROMOTABLE_CANDIDATE
            or any(value is not None for value in holdout_fields)
        ):
            raise M2RoundEvidenceError(
                "zero_promotion_path_invalid",
                "zero-promotion Round must omit Holdout and FWER evidence",
            )
        return
    if (
        round_authority.state is not SearchRoundState.HOLDOUT_BARRIER
        or search_decision.holdout_family_hash != round_authority.holdout_family_hash
        or round_authority.holdout_plan_hash is None
        or round_authority.holdout_reveal_evidence_hash is None
        or holdout_barrier is None
        or multiple_comparison is None
    ):
        raise M2RoundEvidenceError(
            "holdout_evidence_incomplete",
            "promoted Round requires revealed Holdout, Barrier, and FWER",
        )
    holdout_ids = tuple(item.candidate_id for item in holdout_barrier.members)
    result_ids = tuple(item.candidate_id for item in multiple_comparison.candidate_results)
    expected_holdout_input_hash = _digest(
        {
            "round_id": str(round_authority.round_id),
            "holdout_family_hash": round_authority.holdout_family_hash,
            "holdout_plan_hash": round_authority.holdout_plan_hash,
            "holdout_reveal_evidence_hash": round_authority.holdout_reveal_evidence_hash,
            "members": [item.model_dump(mode="json") for item in holdout_barrier.members],
        }
    )
    expected_holdout_rule_hash = _digest(
        {
            "rule_version": M2_HOLDOUT_BARRIER_RULE_VERSION,
            "holdout_plan_hash": round_authority.holdout_plan_hash,
        }
    )
    if (
        holdout_barrier.round_id != round_authority.round_id
        or holdout_barrier.phase is not RoundPhase.HOLDOUT
        or holdout_barrier.input_family_hash != round_authority.holdout_family_hash
        or holdout_barrier.input_summary_hash != expected_holdout_input_hash
        or holdout_barrier.rule_version != M2_HOLDOUT_BARRIER_RULE_VERSION
        or holdout_barrier.rule_hash != expected_holdout_rule_hash
        or holdout_ids != promoted
        or multiple_comparison.round_id != round_authority.round_id
        or multiple_comparison.holdout_barrier_id != holdout_barrier.barrier_id
        or multiple_comparison.holdout_family_hash != round_authority.holdout_family_hash
        or result_ids != holdout_ids
        or multiple_comparison.m != len(holdout_ids)
        or multiple_comparison.protocol_version != M2_FWER_PROTOCOL_VERSION
        or multiple_comparison.protocol_hash != m2_fwer_protocol_hash()
        or recompute_multiple_comparison_result_hash(multiple_comparison)
        != multiple_comparison.result_hash
        or not holdout_barrier.synthetic
        or not multiple_comparison.synthetic
    ):
        raise M2RoundEvidenceError(
            "holdout_evidence_binding_mismatch",
            "Holdout Barrier or FWER belongs to another frozen Family",
        )


def _candidate_evidence(
    *,
    search_barrier: RoundBarrierResult,
    holdout_barrier: RoundBarrierResult | None,
) -> tuple[RoundCandidateEvidence, ...]:
    holdout_by_id = (
        {item.candidate_id: item for item in holdout_barrier.members}
        if holdout_barrier is not None
        else {}
    )
    result: list[RoundCandidateEvidence] = []
    for search_member in search_barrier.members:
        holdout_member = holdout_by_id.get(search_member.candidate_id)
        if holdout_member is not None and (
            holdout_member.round_candidate_id != search_member.round_candidate_id
            or holdout_member.artifact_id != search_member.artifact_id
            or holdout_member.artifact_hash != search_member.artifact_hash
            or holdout_member.correctness_evidence_hash != search_member.correctness_evidence_hash
        ):
            raise M2RoundEvidenceError(
                "holdout_candidate_identity_drift",
                "Holdout member changed Candidate, Artifact, or correctness identity",
            )
        budget_hashes = [search_member.budget_usage_evidence_hash]
        cleanup_hashes = []
        if search_member.cleanup_evidence_hash is not None:
            cleanup_hashes.append(search_member.cleanup_evidence_hash)
        if holdout_member is not None:
            budget_hashes.append(holdout_member.budget_usage_evidence_hash)
            if holdout_member.cleanup_evidence_hash is not None:
                cleanup_hashes.append(holdout_member.cleanup_evidence_hash)
        result.append(
            RoundCandidateEvidence(
                round_candidate_id=search_member.round_candidate_id,
                candidate_id=search_member.candidate_id,
                search_member_state=search_member.candidate_state,
                search_evidence_hash=_document_hash(search_member),
                holdout_member_state=holdout_member.candidate_state
                if holdout_member is not None
                else None,
                holdout_evidence_hash=_document_hash(holdout_member)
                if holdout_member is not None
                else None,
                budget_evidence_hashes=tuple(budget_hashes),
                cleanup_evidence_hashes=tuple(cleanup_hashes),
            )
        )
    return tuple(result)


def _read_and_verify_index(
    *,
    reader: EvidenceByteReader,
    uri: str,
    expected_hash: str,
    round_id: UUID,
    requirements: tuple[M2EvidenceRequirement, ...],
) -> M2EvidenceIndex:
    try:
        encoded = reader.read_raw_bytes(uri, expected_hash)
        if _bytes_hash(encoded) != expected_hash:
            raise M2RoundEvidenceError(
                "evidence_index_hash_mismatch", "Evidence Index Hash changed"
            )
        index = M2EvidenceIndex.model_validate_json(encoded)
    except M2RoundEvidenceError:
        raise
    except (ValidationError, ValueError, OSError) as exc:
        raise M2RoundEvidenceError(
            "evidence_index_unreadable", "Evidence Index cannot be verified"
        ) from exc
    if canonical_json_bytes(index) != encoded:
        raise M2RoundEvidenceError(
            "evidence_index_noncanonical", "Evidence Index must use canonical JSON"
        )
    if index.round_id != round_id:
        raise M2RoundEvidenceError(
            "evidence_index_round_mismatch", "Evidence Index belongs to another Round"
        )
    entries = _flatten_entries(index.entries)
    by_role: dict[str, M2EvidenceIndexEntry] = {}
    uris: set[str] = set()
    hashes: dict[str, str] = {}
    for entry in entries:
        if entry.role in by_role or entry.uri in uris:
            raise M2RoundEvidenceError(
                "evidence_index_identity_reused",
                "Evidence Index reuses a role or URI",
            )
        existing_role = hashes.get(entry.sha256)
        if existing_role is not None and existing_role != entry.role:
            raise M2RoundEvidenceError(
                "evidence_index_identity_reused",
                "Evidence Index reuses one Hash across different roles",
            )
        by_role[entry.role] = entry
        uris.add(entry.uri)
        hashes[entry.sha256] = entry.role
    expected_by_role = {item.role: item for item in requirements}
    if set(by_role) != set(expected_by_role):
        raise M2RoundEvidenceError(
            "evidence_index_incomplete",
            "Evidence Index roles differ from the complete Round evidence set",
        )
    for role, requirement in expected_by_role.items():
        entry = by_role[role]
        if entry.sha256 != requirement.sha256 or entry.evidence_type != requirement.evidence_type:
            raise M2RoundEvidenceError(
                "evidence_index_binding_mismatch",
                f"Evidence Index entry changed identity for {role}",
            )
        try:
            evidence_bytes = reader.read_raw_bytes(entry.uri, entry.sha256)
        except M2RoundEvidenceError:
            raise
        except (ValueError, OSError) as exc:
            raise M2RoundEvidenceError(
                "evidence_unavailable", f"Evidence is unavailable for {role}"
            ) from exc
        if _bytes_hash(evidence_bytes) != entry.sha256:
            raise M2RoundEvidenceError(
                "evidence_hash_mismatch", f"Evidence Hash changed for {role}"
            )
        if requirement.expected_bytes is not None and evidence_bytes != requirement.expected_bytes:
            raise M2RoundEvidenceError(
                "evidence_document_mismatch",
                f"typed evidence content changed for {role}",
            )
    return index


def _flatten_entries(
    roots: tuple[M2EvidenceIndexEntry, ...],
) -> tuple[M2EvidenceIndexEntry, ...]:
    result: list[M2EvidenceIndexEntry] = []
    stack = [(entry, 1) for entry in reversed(roots)]
    while stack:
        entry, depth = stack.pop()
        if depth > MAX_EVIDENCE_INDEX_DEPTH:
            raise M2RoundEvidenceError(
                "evidence_index_too_deep", "Evidence Index exceeds recursion limit"
            )
        result.append(entry)
        if len(result) > MAX_EVIDENCE_INDEX_ENTRIES:
            raise M2RoundEvidenceError(
                "evidence_index_too_large", "Evidence Index contains too many entries"
            )
        stack.extend((child, depth + 1) for child in reversed(entry.children))
    return tuple(result)


def _add_member_requirements(
    requirements: dict[str, M2EvidenceRequirement],
    *,
    member: BarrierMemberResult,
    phase: Literal["search", "holdout"],
) -> None:
    candidate = str(member.candidate_id)
    _add_document(
        requirements,
        role=f"candidate/{candidate}/{phase}/member",
        evidence_type="barrier_member",
        value=member,
    )
    if member.artifact_hash is not None:
        _add_requirement(
            requirements,
            role=f"candidate/{candidate}/artifact",
            evidence_type="candidate_artifact",
            sha256=member.artifact_hash,
        )
    if member.correctness_evidence_hash is not None:
        _add_requirement(
            requirements,
            role=f"candidate/{candidate}/correctness",
            evidence_type="correctness_evidence",
            sha256=member.correctness_evidence_hash,
        )
    _add_requirement(
        requirements,
        role=f"candidate/{candidate}/{phase}/budget-usage",
        evidence_type="budget_usage_evidence",
        sha256=member.budget_usage_evidence_hash,
    )
    if member.cleanup_evidence_hash is not None:
        _add_requirement(
            requirements,
            role=f"candidate/{candidate}/{phase}/cleanup",
            evidence_type="cleanup_evidence",
            sha256=member.cleanup_evidence_hash,
        )
    if member.failure_evidence_hash is not None:
        _add_requirement(
            requirements,
            role=f"candidate/{candidate}/{phase}/failure",
            evidence_type="failure_evidence",
            sha256=member.failure_evidence_hash,
        )


def _add_document(
    requirements: dict[str, M2EvidenceRequirement],
    *,
    role: str,
    evidence_type: str,
    value: object,
) -> None:
    encoded = canonical_json_bytes(value)
    _add_requirement(
        requirements,
        role=role,
        evidence_type=evidence_type,
        sha256="sha256:" + hashlib.sha256(encoded).hexdigest(),
        expected_bytes=encoded,
    )


def _add_requirement(
    requirements: dict[str, M2EvidenceRequirement],
    *,
    role: str,
    evidence_type: str,
    sha256: str,
    expected_bytes: bytes | None = None,
) -> None:
    requirement = M2EvidenceRequirement(
        role=role,
        evidence_type=evidence_type,
        sha256=sha256,
        expected_bytes=expected_bytes,
    )
    existing = requirements.get(role)
    if existing is not None and existing != requirement:
        raise M2RoundEvidenceError(
            "evidence_requirement_conflict", f"Evidence role conflicts: {role}"
        )
    requirements[role] = requirement


def _document_hash(value: object) -> str:
    return _bytes_hash(canonical_json_bytes(value))


def _bytes_hash(encoded: bytes) -> str:
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(_jsonable(value))).hexdigest()


def _jsonable(value: object) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value
