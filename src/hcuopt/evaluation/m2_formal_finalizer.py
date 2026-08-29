# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal, Protocol
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, ValidationError, field_validator, model_validator

from hcuopt.contracts.m2 import SearchRound
from hcuopt.contracts.m2_formal_authority_v1 import FormalAuthorityContextDescriptor
from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.domain.enums import (
    RoundBarrierOutcome,
    RoundCandidateState,
    RoundPhase,
    RoundTerminalReason,
    SearchRoundRunMode,
    SearchRoundState,
)
from hcuopt.evaluation.m2_formal_authority import FormalHoldoutRevealPersistence
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
    holdout_family_hash,
    m2_fwer_protocol_hash,
    recompute_multiple_comparison_result_hash,
)
from hcuopt.evaluation.m2_verifier import (
    MAX_EVIDENCE_INDEX_DEPTH,
    MAX_EVIDENCE_INDEX_ENTRIES,
    EvidenceByteReader,
    M2RoundEvidenceError,
)
from hcuopt.measurement.evidence import canonical_json_bytes

M2_FORMAL_EVIDENCE_INDEX_SCHEMA_VERSION = "m2a-formal-evidence-index-v1"
FormalEvidenceProducerRole = Literal[
    "measurement_producer",
    "independent_verifier",
    "plan_authority",
    "control_plane",
    "build_producer",
]


def _require_local_file_uri(uri: str, *, label: str) -> str:
    parsed = urlparse(uri)
    if (
        parsed.scheme != "file"
        or parsed.netloc not in {"", "localhost"}
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{label} requires an absolute local file URI")
    return uri


class FormalM2EvidenceIndexEntry(FrozenEvaluationModel):
    role: str = Field(pattern=r"^[a-z0-9][a-z0-9_./-]{0,299}$")
    evidence_type: str = Field(min_length=1, max_length=200)
    uri: str = Field(min_length=1, max_length=4000)
    sha256: str = Field(pattern=SHA256_PATTERN)
    producer_role: FormalEvidenceProducerRole
    producer_id: str = Field(min_length=1, max_length=300)
    producer_hash: str = Field(pattern=SHA256_PATTERN)
    retention_owner: str = Field(min_length=1, max_length=300)
    accessibility_status: Literal["verified"] = "verified"
    accessibility_checked_at: datetime
    children: tuple[FormalM2EvidenceIndexEntry, ...] = Field(default=(), max_length=64)

    @field_validator("children", mode="before")
    @classmethod
    def freeze_children(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("uri")
    @classmethod
    def require_local_uri(cls, value: str) -> str:
        return _require_local_file_uri(value, label="Formal Evidence entry")

    @model_validator(mode="after")
    def require_deterministic_children(self) -> FormalM2EvidenceIndexEntry:
        if (
            self.accessibility_checked_at.tzinfo is None
            or self.accessibility_checked_at.utcoffset() is None
        ):
            raise ValueError("Formal Evidence accessibility time must be timezone-aware")
        roles = tuple(item.role for item in self.children)
        if len(set(roles)) != len(roles) or roles != tuple(sorted(roles)):
            raise ValueError("Formal Evidence children must be unique and ordered")
        return self


class FormalM2EvidenceIndex(FrozenEvaluationModel):
    schema_version: Literal["m2a-formal-evidence-index-v1"] = (
        M2_FORMAL_EVIDENCE_INDEX_SCHEMA_VERSION
    )
    round_id: UUID
    authority_context_id: UUID
    authority_context_hash: str = Field(pattern=SHA256_PATTERN)
    evidence_store_id: str = Field(min_length=3, max_length=200)
    evidence_store_hash: str = Field(pattern=SHA256_PATTERN)
    entries: tuple[FormalM2EvidenceIndexEntry, ...] = Field(min_length=1, max_length=64)
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False
    created_at: datetime

    @field_validator("entries", mode="before")
    @classmethod
    def freeze_entries(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_deterministic_root(self) -> FormalM2EvidenceIndex:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("Formal Evidence Index time must be timezone-aware")
        roles = tuple(item.role for item in self.entries)
        if len(set(roles)) != len(roles) or roles != tuple(sorted(roles)):
            raise ValueError("Formal Evidence root entries must be unique and ordered")
        return self


@dataclass(frozen=True, slots=True)
class FormalM2EvidenceRequirement:
    role: str
    evidence_type: str
    sha256: str
    producer_role: FormalEvidenceProducerRole
    producer_id: str | None = None
    producer_hash: str | None = None
    expected_bytes: bytes | None = None


class M2FormalRoundFinalizationService(Protocol):
    def verify(
        self,
        *,
        context: FormalAuthorityContextDescriptor,
        round_authority: SearchRound,
        search_barrier: RoundBarrierResult,
        holdout_reveal: FormalHoldoutRevealPersistence | None,
        holdout_barrier: RoundBarrierResult | None,
        multiple_comparison: MultipleComparisonResult | None,
        evidence_bundle: RoundEvidenceBundle,
    ) -> None: ...


class M2FormalRoundFinalizer:
    """Rebuild one non-Synthetic Bundle from protected evidence before DB commit."""

    def __init__(self, evidence_reader: EvidenceByteReader) -> None:
        self.evidence_reader = evidence_reader

    def verify(
        self,
        *,
        context: FormalAuthorityContextDescriptor,
        round_authority: SearchRound,
        search_barrier: RoundBarrierResult,
        holdout_reveal: FormalHoldoutRevealPersistence | None,
        holdout_barrier: RoundBarrierResult | None,
        multiple_comparison: MultipleComparisonResult | None,
        evidence_bundle: RoundEvidenceBundle,
    ) -> None:
        rebuilt = build_formal_round_evidence(
            context=context,
            round_authority=round_authority,
            search_barrier=search_barrier,
            budget_ledger_hash=evidence_bundle.budget_ledger_hash,
            evidence_index_uri=evidence_bundle.evidence_index_uri,
            evidence_index_hash=evidence_bundle.evidence_index_hash,
            evidence_reader=self.evidence_reader,
            holdout_reveal=holdout_reveal,
            holdout_barrier=holdout_barrier,
            multiple_comparison=multiple_comparison,
        )
        if canonical_json_bytes(rebuilt) != canonical_json_bytes(evidence_bundle):
            raise M2RoundEvidenceError(
                "formal_round_evidence_bundle_mismatch",
                "persisted Formal inputs do not rebuild the submitted Evidence Bundle",
            )


def formal_round_evidence_requirements(
    *,
    context: FormalAuthorityContextDescriptor,
    round_authority: SearchRound,
    search_barrier: RoundBarrierResult,
    budget_ledger_hash: str,
    holdout_reveal: FormalHoldoutRevealPersistence | None = None,
    holdout_barrier: RoundBarrierResult | None = None,
    multiple_comparison: MultipleComparisonResult | None = None,
) -> tuple[FormalM2EvidenceRequirement, ...]:
    _validate_formal_round_path(
        context=context,
        round_authority=round_authority,
        search_barrier=search_barrier,
        holdout_reveal=holdout_reveal,
        holdout_barrier=holdout_barrier,
        multiple_comparison=multiple_comparison,
    )
    requirements: dict[str, FormalM2EvidenceRequirement] = {}
    _add_requirement(
        requirements,
        role="search/plan",
        evidence_type="search_plan",
        sha256=round_authority.search_plan_hash,
        producer_role="control_plane",
    )
    _add_requirement(
        requirements,
        role="search/input-summary",
        evidence_type="search_barrier_input",
        sha256=search_barrier.input_summary_hash,
        producer_role="independent_verifier",
        producer_id=context.verifier.verifier_id,
        producer_hash=context.verifier.verifier_hash,
    )
    _add_document(
        requirements,
        role="search/barrier",
        evidence_type="round_barrier",
        value=search_barrier,
        context=context,
    )
    _add_requirement(
        requirements,
        role="budget/ledger",
        evidence_type="round_budget_ledger",
        sha256=budget_ledger_hash,
        producer_role="control_plane",
    )
    for member in search_barrier.members:
        _add_member_requirements(
            requirements,
            member=member,
            phase="search",
            context=context,
        )

    if holdout_barrier is not None and multiple_comparison is not None:
        assert holdout_reveal is not None
        _add_requirement(
            requirements,
            role="holdout/plan",
            evidence_type="holdout_plan",
            sha256=holdout_reveal.holdout_plan_hash,
            producer_role="plan_authority",
            producer_id=context.holdout_plan_authority_id,
            producer_hash=context.holdout_plan_authority_hash,
        )
        _add_requirement(
            requirements,
            role="holdout/reveal",
            evidence_type="holdout_reveal",
            sha256=holdout_reveal.reveal_evidence_hash,
            producer_role="independent_verifier",
            producer_id=context.verifier.verifier_id,
            producer_hash=context.verifier.verifier_hash,
        )
        _add_document(
            requirements,
            role="holdout/barrier",
            evidence_type="round_barrier",
            value=holdout_barrier,
            context=context,
        )
        _add_document(
            requirements,
            role="holdout/multiple-comparison",
            evidence_type="multiple_comparison",
            value=multiple_comparison,
            context=context,
        )
        for member in holdout_barrier.members:
            _add_member_requirements(
                requirements,
                member=member,
                phase="holdout",
                context=context,
            )
        for result in multiple_comparison.candidate_results:
            prefix = f"candidate/{result.candidate_id}/holdout"
            if result.raw_evidence_hash is not None:
                _add_requirement(
                    requirements,
                    role=f"{prefix}/raw-measurement",
                    evidence_type="formal_phase_raw_evidence",
                    sha256=result.raw_evidence_hash,
                    producer_role="measurement_producer",
                )
            if result.baseline_sample_set_hash is not None:
                _add_requirement(
                    requirements,
                    role=f"{prefix}/baseline-samples",
                    evidence_type="baseline_sample_set",
                    sha256=result.baseline_sample_set_hash,
                    producer_role="measurement_producer",
                )
    return tuple(requirements[key] for key in sorted(requirements))


def build_formal_round_evidence(
    *,
    context: FormalAuthorityContextDescriptor,
    round_authority: SearchRound,
    search_barrier: RoundBarrierResult,
    budget_ledger_hash: str,
    evidence_index_uri: str,
    evidence_index_hash: str,
    evidence_reader: EvidenceByteReader,
    holdout_reveal: FormalHoldoutRevealPersistence | None = None,
    holdout_barrier: RoundBarrierResult | None = None,
    multiple_comparison: MultipleComparisonResult | None = None,
) -> RoundEvidenceBundle:
    requirements = formal_round_evidence_requirements(
        context=context,
        round_authority=round_authority,
        search_barrier=search_barrier,
        budget_ledger_hash=budget_ledger_hash,
        holdout_reveal=holdout_reveal,
        holdout_barrier=holdout_barrier,
        multiple_comparison=multiple_comparison,
    )
    index = _read_and_verify_formal_index(
        reader=evidence_reader,
        uri=evidence_index_uri,
        expected_hash=evidence_index_hash,
        context=context,
        requirements=requirements,
    )
    candidate_evidence = _candidate_evidence(
        search_barrier=search_barrier,
        holdout_barrier=holdout_barrier,
    )
    terminal_reason = (
        RoundTerminalReason.HOLDOUT_COMPLETED
        if holdout_barrier is not None
        else RoundTerminalReason.NO_PROMOTABLE_CANDIDATE
    )
    verdict_counts: dict[str, int] = {}
    recommended = None
    if multiple_comparison is not None:
        for result in multiple_comparison.candidate_results:
            verdict_counts[result.verdict.value] = verdict_counts.get(result.verdict.value, 0) + 1
        recommended = multiple_comparison.recommended_candidate_id
    summary = {
        "performance_conclusion": "formal_single_operation_only",
        "evidence_authority": "independent_formal_verifier",
        "terminal_reason": terminal_reason.value,
        "candidate_count": len(candidate_evidence),
        "holdout_member_count": len(holdout_barrier.members) if holdout_barrier else 0,
        "verdict_counts": verdict_counts,
        "recommended_candidate_id": str(recommended) if recommended else None,
        "scope_warning": "not a model, service, or end-to-end performance conclusion",
        "automatic_release_allowed": False,
    }
    values = {
        "round_id": round_authority.round_id,
        "task_id": round_authority.task_id,
        "run_mode": SearchRoundRunMode.FORMAL,
        "terminal_reason": terminal_reason,
        "candidate_family_hash": context.candidate_family_hash,
        "artifact_family_hash": context.artifact_family_hash,
        "holdout_family_hash": holdout_reveal.holdout_family_hash
        if holdout_reveal is not None
        else None,
        "search_plan_hash": context.search_plan_hash,
        "holdout_plan_commitment": context.holdout_plan_commitment,
        "holdout_plan_hash": holdout_reveal.holdout_plan_hash
        if holdout_reveal is not None
        else None,
        "holdout_reveal_evidence_hash": holdout_reveal.reveal_evidence_hash
        if holdout_reveal is not None
        else None,
        "candidate_evidence": candidate_evidence,
        "search_barrier_id": search_barrier.barrier_id,
        "holdout_barrier_id": holdout_barrier.barrier_id if holdout_barrier else None,
        "multiple_comparison_id": multiple_comparison.multiple_comparison_id
        if multiple_comparison
        else None,
        "budget_ledger_hash": budget_ledger_hash,
        "evidence_index_uri": evidence_index_uri,
        "evidence_index_hash": evidence_index_hash,
        "summary": summary,
        "synthetic": False,
        "automatic_release_allowed": False,
    }
    input_digest = _digest(values)
    return RoundEvidenceBundle(
        round_evidence_bundle_id=uuid5(
            NAMESPACE_URL, f"hcuopt:m2-formal-round-evidence:{input_digest}"
        ),
        created_at=index.created_at,
        **values,
    )


def _validate_formal_round_path(
    *,
    context: FormalAuthorityContextDescriptor,
    round_authority: SearchRound,
    search_barrier: RoundBarrierResult,
    holdout_reveal: FormalHoldoutRevealPersistence | None,
    holdout_barrier: RoundBarrierResult | None,
    multiple_comparison: MultipleComparisonResult | None,
) -> None:
    if (
        round_authority.run_mode is not SearchRoundRunMode.FORMAL
        or round_authority.automatic_release_allowed
        or context.round_id != round_authority.round_id
        or context.task_id != round_authority.task_id
        or context.target_snapshot_id != round_authority.target_snapshot_id
        or context.stage0_run_id != round_authority.stage0_run_id
        or context.stage0_protocol_hash != round_authority.stage0_protocol_hash
        or context.baseline_epoch_id != round_authority.baseline_epoch_id
        or context.hotspot_id != round_authority.hotspot_id
        or context.candidate_family_hash != round_authority.candidate_family_hash
        or context.artifact_family_hash != round_authority.artifact_family_hash
        or context.search_plan_hash != round_authority.search_plan_hash
        or context.holdout_plan_commitment != round_authority.holdout_plan_commitment
        or context.selection_rule_hash != round_authority.selection_rule_hash
        or search_barrier.run_mode is not SearchRoundRunMode.FORMAL
        or search_barrier.synthetic
        or search_barrier.round_id != round_authority.round_id
        or search_barrier.phase is not RoundPhase.SEARCH
        or search_barrier.input_family_hash != context.artifact_family_hash
        or search_barrier.rule_hash != context.selection_rule_hash
        or search_barrier.closed_by != context.verifier.verifier_id
        or len(search_barrier.members) != round_authority.declared_candidate_count
    ):
        raise M2RoundEvidenceError(
            "formal_round_evidence_binding_mismatch",
            "Formal Search Barrier disagrees with the sealed Authority Context",
        )
    promoted = search_barrier.promoted_candidate_ids
    phase_failures = {
        RoundCandidateState.SEARCH_FAILED,
        RoundCandidateState.HOLDOUT_FAILED,
    }
    if any(
        item.candidate_state in phase_failures and item.cleanup_evidence_hash is None
        for item in search_barrier.members
    ):
        raise M2RoundEvidenceError(
            "formal_cleanup_evidence_incomplete",
            "Formal measurement failures require terminal cleanup evidence",
        )
    if not promoted:
        if (
            round_authority.state is not SearchRoundState.SEARCH_BARRIER
            or search_barrier.outcome is not RoundBarrierOutcome.NO_PROMOTABLE_CANDIDATE
            or any(
                value is not None
                for value in (holdout_reveal, holdout_barrier, multiple_comparison)
            )
        ):
            raise M2RoundEvidenceError(
                "formal_zero_promotion_path_invalid",
                "zero-promotion Formal Round must omit Reveal, Holdout, and FWER",
            )
        return
    if (
        round_authority.state is not SearchRoundState.HOLDOUT_BARRIER
        or search_barrier.outcome is not RoundBarrierOutcome.MEMBERS_PROMOTED
        or holdout_reveal is None
        or holdout_barrier is None
        or multiple_comparison is None
        or holdout_reveal.context.authority_context_id != context.authority_context_id
        or holdout_reveal.context.context_hash != context.context_hash
        or holdout_reveal.search_barrier_id != search_barrier.barrier_id
        or holdout_reveal.holdout_family_hash != round_authority.holdout_family_hash
        or holdout_reveal.holdout_plan_hash != round_authority.holdout_plan_hash
        or holdout_reveal.reveal_evidence_hash
        != round_authority.holdout_reveal_evidence_hash
        or holdout_reveal.revealed_by != context.verifier.verifier_id
        or holdout_barrier.run_mode is not SearchRoundRunMode.FORMAL
        or holdout_barrier.synthetic
        or holdout_barrier.round_id != round_authority.round_id
        or holdout_barrier.phase is not RoundPhase.HOLDOUT
        or holdout_barrier.input_family_hash != holdout_reveal.holdout_family_hash
        or holdout_barrier.closed_by != context.verifier.verifier_id
        or tuple(item.candidate_id for item in holdout_barrier.members) != promoted
        or multiple_comparison.run_mode is not SearchRoundRunMode.FORMAL
        or multiple_comparison.synthetic
        or multiple_comparison.round_id != round_authority.round_id
        or multiple_comparison.holdout_barrier_id != holdout_barrier.barrier_id
        or multiple_comparison.holdout_family_hash != holdout_reveal.holdout_family_hash
        or multiple_comparison.protocol_version != M2_FWER_PROTOCOL_VERSION
        or multiple_comparison.protocol_hash != m2_fwer_protocol_hash()
        or multiple_comparison.result_hash
        != recompute_multiple_comparison_result_hash(multiple_comparison)
        or tuple(item.candidate_id for item in multiple_comparison.candidate_results)
        != promoted
    ):
        raise M2RoundEvidenceError(
            "formal_holdout_evidence_binding_mismatch",
            "Formal Reveal, Holdout Barrier, or FWER changed the frozen Family",
        )
    promoted_members = tuple(
        item for item in search_barrier.members if item.candidate_id in promoted
    )
    if holdout_reveal.holdout_family_hash != holdout_family_hash(
        round_authority=round_authority,
        members=promoted_members,
    ):
        raise M2RoundEvidenceError(
            "formal_holdout_family_hash_mismatch",
            "Formal Holdout Family does not match promoted Candidate identities",
        )
    if any(
        item.candidate_state in phase_failures and item.cleanup_evidence_hash is None
        for item in holdout_barrier.members
    ):
        raise M2RoundEvidenceError(
            "formal_cleanup_evidence_incomplete",
            "Formal measurement failures require terminal cleanup evidence",
        )


def _read_and_verify_formal_index(
    *,
    reader: EvidenceByteReader,
    uri: str,
    expected_hash: str,
    context: FormalAuthorityContextDescriptor,
    requirements: tuple[FormalM2EvidenceRequirement, ...],
) -> FormalM2EvidenceIndex:
    _require_local_file_uri(uri, label="Formal Evidence Index")
    try:
        encoded = reader.read_raw_bytes(uri, expected_hash)
        if _bytes_hash(encoded) != expected_hash:
            raise M2RoundEvidenceError(
                "formal_evidence_index_hash_mismatch", "Formal Index Hash changed"
            )
        index = FormalM2EvidenceIndex.model_validate_json(encoded)
    except M2RoundEvidenceError:
        raise
    except (ValidationError, ValueError, OSError) as exc:
        raise M2RoundEvidenceError(
            "formal_evidence_index_unreadable",
            "Formal Evidence Index cannot be verified",
        ) from exc
    if canonical_json_bytes(index) != encoded:
        raise M2RoundEvidenceError(
            "formal_evidence_index_noncanonical", "Formal Index must be canonical JSON"
        )
    if (
        index.round_id != context.round_id
        or index.authority_context_id != context.authority_context_id
        or index.authority_context_hash != context.context_hash
        or index.evidence_store_id != context.evidence_store.store_id
        or index.evidence_store_hash != context.evidence_store.store_hash
    ):
        raise M2RoundEvidenceError(
            "formal_evidence_index_context_mismatch",
            "Formal Evidence Index belongs to another Context or Store",
        )
    entries = _flatten_entries(index.entries)
    by_role: dict[str, FormalM2EvidenceIndexEntry] = {}
    uris: set[str] = set()
    hashes: dict[str, str] = {}
    for entry in entries:
        if entry.role in by_role or entry.uri in uris:
            raise M2RoundEvidenceError(
                "formal_evidence_index_identity_reused",
                "Formal Index reuses a role or URI",
            )
        existing_role = hashes.get(entry.sha256)
        if existing_role is not None and existing_role != entry.role:
            raise M2RoundEvidenceError(
                "formal_evidence_index_identity_reused",
                "Formal Index reuses one Hash across different roles",
            )
        by_role[entry.role] = entry
        uris.add(entry.uri)
        hashes[entry.sha256] = entry.role
    expected_by_role = {item.role: item for item in requirements}
    if set(by_role) != set(expected_by_role):
        raise M2RoundEvidenceError(
            "formal_evidence_index_incomplete",
            "Formal Index roles differ from the complete Round evidence set",
        )
    for role, requirement in expected_by_role.items():
        entry = by_role[role]
        if (
            entry.sha256 != requirement.sha256
            or entry.evidence_type != requirement.evidence_type
            or entry.producer_role != requirement.producer_role
            or (
                requirement.producer_id is not None
                and entry.producer_id != requirement.producer_id
            )
            or (
                requirement.producer_hash is not None
                and entry.producer_hash != requirement.producer_hash
            )
        ):
            raise M2RoundEvidenceError(
                "formal_evidence_index_binding_mismatch",
                f"Formal Index changed identity or authority for {role}",
            )
        try:
            evidence_bytes = reader.read_raw_bytes(entry.uri, entry.sha256)
        except M2RoundEvidenceError:
            raise
        except (ValueError, OSError) as exc:
            raise M2RoundEvidenceError(
                "formal_evidence_unavailable", f"Evidence is unavailable for {role}"
            ) from exc
        if _bytes_hash(evidence_bytes) != entry.sha256:
            raise M2RoundEvidenceError(
                "formal_evidence_hash_mismatch", f"Evidence Hash changed for {role}"
            )
        if requirement.expected_bytes is not None and evidence_bytes != requirement.expected_bytes:
            raise M2RoundEvidenceError(
                "formal_evidence_document_mismatch",
                f"typed evidence content changed for {role}",
            )
    return index


def _flatten_entries(
    roots: tuple[FormalM2EvidenceIndexEntry, ...],
) -> tuple[FormalM2EvidenceIndexEntry, ...]:
    result: list[FormalM2EvidenceIndexEntry] = []
    stack = [(entry, 1) for entry in reversed(roots)]
    while stack:
        entry, depth = stack.pop()
        if depth > MAX_EVIDENCE_INDEX_DEPTH:
            raise M2RoundEvidenceError(
                "formal_evidence_index_too_deep", "Formal Index exceeds recursion limit"
            )
        result.append(entry)
        if len(result) > MAX_EVIDENCE_INDEX_ENTRIES:
            raise M2RoundEvidenceError(
                "formal_evidence_index_too_large", "Formal Index has too many entries"
            )
        stack.extend((child, depth + 1) for child in reversed(entry.children))
    return tuple(result)


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
    result = []
    for search_member in search_barrier.members:
        holdout_member = holdout_by_id.get(search_member.candidate_id)
        if holdout_member is not None and (
            holdout_member.round_candidate_id != search_member.round_candidate_id
            or holdout_member.artifact_id != search_member.artifact_id
            or holdout_member.artifact_hash != search_member.artifact_hash
            or holdout_member.correctness_evidence_hash
            != search_member.correctness_evidence_hash
        ):
            raise M2RoundEvidenceError(
                "formal_holdout_candidate_identity_drift",
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
                if holdout_member
                else None,
                holdout_evidence_hash=_document_hash(holdout_member)
                if holdout_member
                else None,
                budget_evidence_hashes=tuple(budget_hashes),
                cleanup_evidence_hashes=tuple(cleanup_hashes),
            )
        )
    return tuple(result)


def _add_member_requirements(
    requirements: dict[str, FormalM2EvidenceRequirement],
    *,
    member: BarrierMemberResult,
    phase: Literal["search", "holdout"],
    context: FormalAuthorityContextDescriptor,
) -> None:
    candidate = str(member.candidate_id)
    _add_document(
        requirements,
        role=f"candidate/{candidate}/{phase}/member",
        evidence_type="barrier_member",
        value=member,
        context=context,
    )
    if member.artifact_hash is not None:
        _add_requirement(
            requirements,
            role=f"candidate/{candidate}/artifact",
            evidence_type="candidate_artifact",
            sha256=member.artifact_hash,
            producer_role="build_producer",
        )
    if member.correctness_evidence_hash is not None:
        _add_requirement(
            requirements,
            role=f"candidate/{candidate}/correctness",
            evidence_type="correctness_evidence",
            sha256=member.correctness_evidence_hash,
            producer_role="independent_verifier",
            producer_id=context.verifier.verifier_id,
            producer_hash=context.verifier.verifier_hash,
        )
    _add_requirement(
        requirements,
        role=f"candidate/{candidate}/{phase}/budget-usage",
        evidence_type="budget_usage_evidence",
        sha256=member.budget_usage_evidence_hash,
        producer_role="control_plane",
    )
    if member.cleanup_evidence_hash is not None:
        _add_requirement(
            requirements,
            role=f"candidate/{candidate}/{phase}/cleanup",
            evidence_type="cleanup_evidence",
            sha256=member.cleanup_evidence_hash,
            producer_role="measurement_producer",
        )
    if member.failure_evidence_hash is not None:
        _add_requirement(
            requirements,
            role=f"candidate/{candidate}/{phase}/failure",
            evidence_type="failure_evidence",
            sha256=member.failure_evidence_hash,
            producer_role="measurement_producer",
        )


def _add_document(
    requirements: dict[str, FormalM2EvidenceRequirement],
    *,
    role: str,
    evidence_type: str,
    value: object,
    context: FormalAuthorityContextDescriptor,
) -> None:
    encoded = canonical_json_bytes(value)
    _add_requirement(
        requirements,
        role=role,
        evidence_type=evidence_type,
        sha256=_bytes_hash(encoded),
        producer_role="independent_verifier",
        producer_id=context.verifier.verifier_id,
        producer_hash=context.verifier.verifier_hash,
        expected_bytes=encoded,
    )


def _add_requirement(
    requirements: dict[str, FormalM2EvidenceRequirement],
    *,
    role: str,
    evidence_type: str,
    sha256: str,
    producer_role: FormalEvidenceProducerRole,
    producer_id: str | None = None,
    producer_hash: str | None = None,
    expected_bytes: bytes | None = None,
) -> None:
    requirement = FormalM2EvidenceRequirement(
        role=role,
        evidence_type=evidence_type,
        sha256=sha256,
        producer_role=producer_role,
        producer_id=producer_id,
        producer_hash=producer_hash,
        expected_bytes=expected_bytes,
    )
    existing = requirements.get(role)
    if existing is not None and existing != requirement:
        raise M2RoundEvidenceError(
            "formal_evidence_requirement_conflict", f"Evidence role conflicts: {role}"
        )
    requirements[role] = requirement


def _document_hash(value: object) -> str:
    return _bytes_hash(canonical_json_bytes(value))


def _bytes_hash(encoded: bytes) -> str:
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _digest(value: object) -> str:
    return _bytes_hash(canonical_json_bytes(_jsonable(value)))


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
