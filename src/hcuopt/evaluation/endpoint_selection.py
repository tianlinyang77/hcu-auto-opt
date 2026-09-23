# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Service selection from a committed Search summary, never Holdout scores."""

from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.domain.enums import ManualCandidateVerdict
from hcuopt.evaluation.m2_models import (
    FrozenEvaluationModel,
    MultipleComparisonResult,
    RoundBarrierResult,
)


class SearchSelectionScore(FrozenEvaluationModel):
    candidate_id: UUID
    round_measurement_ref_id: UUID
    mean_effect: float = Field(allow_inf_nan=False)


class FormalSearchSelectionInput(FrozenEvaluationModel):
    schema_version: Literal["formal-search-selection-v1"] = "formal-search-selection-v1"
    round_id: UUID
    artifact_family_hash: str = Field(pattern=SHA256_PATTERN)
    selection_rule_hash: str = Field(pattern=SHA256_PATTERN)
    ranking_rule: Literal["mean_effect_desc_uuid_asc"] = "mean_effect_desc_uuid_asc"
    scores: tuple[SearchSelectionScore, ...] = Field(max_length=4)

    @field_validator("scores", mode="before")
    @classmethod
    def freeze_scores(cls, value):
        return tuple(value) if isinstance(value, list) else value


def select_endpoint_candidate(
    search: FormalSearchSelectionInput,
    barrier: RoundBarrierResult,
    comparison: MultipleComparisonResult | None,
) -> UUID | None:
    """Caller must verify summary bytes against Barrier.input_summary_hash first."""
    if (
        barrier.synthetic
        or barrier.phase.value != "search"
        or search.round_id != barrier.round_id
        or search.artifact_family_hash != barrier.input_family_hash
        or search.selection_rule_hash != barrier.rule_hash
    ):
        raise ValueError("service selection Search authority differs")
    measured = {
        m.candidate_id: m.round_measurement_ref_id
        for m in barrier.members
        if m.candidate_state.value == "search_measured"
    }
    scores = {score.candidate_id: score for score in search.scores}
    if len(scores) != len(search.scores) or set(scores) != set(measured):
        raise ValueError("service selection requires the complete measured Search family")
    if any(score.round_measurement_ref_id != measured[key] for key, score in scores.items()):
        raise ValueError("service selection Search measurement identity differs")
    if comparison is None:
        if barrier.promoted_candidate_ids:
            raise ValueError("service selection requires batch D for promoted candidates")
        return None
    if comparison.synthetic or comparison.round_id != search.round_id:
        raise ValueError("service selection D authority differs")
    if {r.candidate_id for r in comparison.candidate_results} != set(
        barrier.promoted_candidate_ids
    ):
        raise ValueError("service selection D family differs from Search promotions")
    eligible = {
        r.candidate_id
        for r in comparison.candidate_results
        if r.verdict is ManualCandidateVerdict.FASTER
    }
    ranked = sorted(scores, key=lambda key: (-scores[key].mean_effect, str(key)))
    return next((key for key in ranked if key in eligible), None)
