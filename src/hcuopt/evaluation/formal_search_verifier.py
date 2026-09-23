# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Reuse M1 D's raw-evidence verifiers for Formal Search and Holdout phases."""

from dataclasses import dataclass
from typing import Protocol

from hcuopt.contracts.m2 import RoundCandidate, SearchRound
from hcuopt.domain.enums import RoundCandidateState, RoundPhase, SearchRoundState
from hcuopt.domain.errors import Conflict
from hcuopt.evaluation.m1_protocol import M1HotspotCorrectnessSpec
from hcuopt.evaluation.m1_verifier import (
    M1CorrectnessEvidenceReference,
    M1CorrectnessVerifier,
    M1PerformanceEvidenceReference,
    M1PerformanceVerificationContext,
    M1PerformanceVerificationResult,
    M1PerformanceVerifier,
    M1VerificationContext,
)
from hcuopt.measurement.m2_models import RoundMeasurementRef


@dataclass(frozen=True)
class FormalSearchVerificationMaterials:
    round_authority: SearchRound
    member: RoundCandidate
    correctness_context: M1VerificationContext
    correctness_reference: M1CorrectnessEvidenceReference
    hotspot: M1HotspotCorrectnessSpec
    performance_context: M1PerformanceVerificationContext


class FormalSearchMaterialLoader(Protocol):
    """Deployment-owned loader, not values supplied by a producer or HTTP caller."""

    def load(self, reference: RoundMeasurementRef) -> FormalSearchVerificationMaterials: ...


class FormalSearchVerifier:
    """Independently verify one Search or Holdout measurement reference."""

    def __init__(
        self,
        loader: FormalSearchMaterialLoader,
        correctness_verifier: M1CorrectnessVerifier,
        performance_verifier: M1PerformanceVerifier,
    ):
        self.loader = loader
        self.correctness_verifier = correctness_verifier
        self.performance_verifier = performance_verifier

    def verify(self, reference: RoundMeasurementRef) -> M1PerformanceVerificationResult:
        material = self.loader.load(reference)
        authority, member = material.round_authority, material.member
        context, correctness = material.performance_context, material.correctness_context
        if reference.phase is RoundPhase.SEARCH:
            allowed_round_states = {
                SearchRoundState.SEARCH_MEASURING,
                SearchRoundState.SEARCH_BARRIER,
            }
            allowed_member_states = {
                RoundCandidateState.CORRECTNESS_PASSED,
                RoundCandidateState.SEARCH_MEASURED,
            }
            phase_plan_hash = authority.search_plan_hash
        else:
            allowed_round_states = {
                SearchRoundState.HOLDOUT_MEASURING,
                SearchRoundState.HOLDOUT_BARRIER,
            }
            allowed_member_states = {
                RoundCandidateState.SEARCH_MEASURED,
                RoundCandidateState.HOLDOUT_MEASURED,
            }
            phase_plan_hash = authority.holdout_plan_hash
        if (
            authority.run_mode != "formal"
            or authority.state not in allowed_round_states
            or authority.round_id != reference.round_id
            or member.round_id != authority.round_id
            or member.round_candidate_id != reference.round_candidate_id
            or member.candidate_id != reference.candidate_id
            or member.candidate_kind != "business"
            or member.state not in allowed_member_states
            or reference.candidate_family_hash != authority.candidate_family_hash
            or reference.artifact_family_hash != authority.artifact_family_hash
            or reference.phase_plan_hash != phase_plan_hash
            or (
                reference.phase is RoundPhase.HOLDOUT
                and (
                    reference.holdout_family_hash != authority.holdout_family_hash
                    or reference.holdout_reveal_evidence_hash
                    != authority.holdout_reveal_evidence_hash
                )
            )
        ):
            raise Conflict("Formal phase reference differs from durable Round/member")
        for name in (
            "task_id",
            "baseline_epoch_id",
            "target_snapshot_id",
            "stage0_run_id",
            "stage0_protocol_hash",
            "workload_id",
            "workload_hash",
        ):
            if getattr(context, name) != getattr(authority, name) or getattr(
                correctness, name
            ) != getattr(authority, name):
                raise Conflict(f"Formal Search context differs at {name}")
        for name in (
            "candidate_id",
            "artifact_id",
            "artifact_hash",
            "baseline_source_hash",
            "candidate_source_hash",
        ):
            if getattr(context, name) != getattr(member, name) or getattr(
                correctness, name
            ) != getattr(member, name):
                raise Conflict(f"Formal Search candidate context differs at {name}")
        if (
            context.round_id != authority.round_id
            or context.configuration_hash != authority.configuration_hash
            or context.image_digest != authority.image_digest
            or context.adapter_profile != authority.adapter_profile
            or context.artifact_id != reference.artifact_id
            or context.artifact_hash != reference.artifact_hash
            or context.lease_id != reference.lease_id
            or context.resource_id != reference.resource_id
            or context.fencing_token != reference.fencing_token
        ):
            raise Conflict("Formal Search performance authority differs")
        for name in ("target_id", "target_fingerprint"):
            if getattr(context, name) != getattr(correctness, name):
                raise Conflict(f"Formal Search correctness target differs at {name}")
        # Both verifiers reread protected files and check Hashes. A producer's
        # cached correctness boolean or claimed speedup never enters this call.
        correctness_result = self.correctness_verifier.verify(
            correctness,
            material.hotspot,
            material.correctness_reference,
        )
        return self.performance_verifier.verify(
            correctness_result,
            context,
            M1PerformanceEvidenceReference(
                measurement_id=reference.measurement_id,
                uri=reference.raw_evidence_uri,
                sha256=reference.raw_evidence_hash,
            ),
        )
