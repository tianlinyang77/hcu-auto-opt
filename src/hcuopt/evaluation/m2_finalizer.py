# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from typing import Protocol

from hcuopt.contracts.m2 import SearchRound
from hcuopt.evaluation.m2_models import (
    MultipleComparisonResult,
    RoundBarrierResult,
    RoundEvidenceBundle,
)
from hcuopt.evaluation.m2_statistics import SearchBarrierDecision
from hcuopt.evaluation.m2_verifier import (
    EvidenceByteReader,
    M2RoundEvidenceError,
    build_scripted_round_evidence,
)
from hcuopt.measurement.evidence import canonical_json_bytes


class M2ScriptedRoundFinalizationService(Protocol):
    """D verification boundary consumed by A's transactional finalizer."""

    def verify(
        self,
        *,
        round_authority: SearchRound,
        search_decision: SearchBarrierDecision,
        evidence_bundle: RoundEvidenceBundle,
        holdout_barrier: RoundBarrierResult | None,
        multiple_comparison: MultipleComparisonResult | None,
    ) -> None: ...


class M2ScriptedRoundFinalizer:
    """Rebuild one synthetic Bundle from immutable evidence before DB commit."""

    def __init__(self, evidence_reader: EvidenceByteReader) -> None:
        self.evidence_reader = evidence_reader

    def verify(
        self,
        *,
        round_authority: SearchRound,
        search_decision: SearchBarrierDecision,
        evidence_bundle: RoundEvidenceBundle,
        holdout_barrier: RoundBarrierResult | None,
        multiple_comparison: MultipleComparisonResult | None,
    ) -> None:
        rebuilt = build_scripted_round_evidence(
            round_authority=round_authority,
            search_decision=search_decision,
            budget_ledger_hash=evidence_bundle.budget_ledger_hash,
            evidence_index_uri=evidence_bundle.evidence_index_uri,
            evidence_index_hash=evidence_bundle.evidence_index_hash,
            evidence_reader=self.evidence_reader,
            holdout_barrier=holdout_barrier,
            multiple_comparison=multiple_comparison,
        )
        if canonical_json_bytes(rebuilt) != canonical_json_bytes(evidence_bundle):
            raise M2RoundEvidenceError(
                "round_evidence_bundle_mismatch",
                "persisted Round inputs do not rebuild the submitted Evidence Bundle",
            )
