# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import pytest
from pydantic import ValidationError
from test_m2_formal_finalizer import _holdout_fixture, _publish_index

from hcuopt.domain.enums import ManualCandidateVerdict
from hcuopt.evaluation.endpoint_selection import (
    FormalSearchSelectionInput,
    SearchSelectionScore,
    select_endpoint_candidate,
)
from hcuopt.evaluation.m2_formal_finalizer import (
    build_formal_round_evidence,
    formal_round_evidence_requirements,
)


def selection_input(barrier):
    return FormalSearchSelectionInput(
        round_id=barrier.round_id,
        artifact_family_hash=barrier.input_family_hash,
        selection_rule_hash=barrier.rule_hash,
        scores=tuple(
            SearchSelectionScore(
                candidate_id=m.candidate_id,
                round_measurement_ref_id=m.round_measurement_ref_id,
                mean_effect=0.2 - i * 0.1,
            )
            for i, m in enumerate(barrier.members)
        ),
    )


def test_selection_uses_search_not_holdout_order():
    _, _, _, barrier, _, _, comparison, _ = _holdout_fixture()
    search = selection_input(barrier)
    ids = tuple(m.candidate_id for m in barrier.members)
    barrier = barrier.model_copy(update={"promoted_candidate_ids": ids})
    result = comparison.candidate_results[0]
    comparison = comparison.model_copy(
        update={
            "candidate_results": tuple(
                result.model_copy(
                    update={
                        "candidate_id": key,
                        "verdict": ManualCandidateVerdict.FASTER,
                        "adjusted_ci_lower": 0.05 + i * 0.1,
                    }
                )
                for i, key in enumerate(ids)
            ),
            "recommended_candidate_id": ids[-1],
        }
    )
    assert select_endpoint_candidate(search, barrier, comparison) == ids[0]
    reversed_scores = search.model_copy(update={"scores": tuple(reversed(search.scores))})
    assert select_endpoint_candidate(reversed_scores, barrier, comparison) == ids[0]
    rejected_first = comparison.model_copy(
        update={
            "candidate_results": (
                comparison.candidate_results[0].model_copy(
                    update={
                        "verdict": ManualCandidateVerdict.SLOWER,
                    }
                ),
                comparison.candidate_results[1],
            )
        }
    )
    assert select_endpoint_candidate(search, barrier, rejected_first) == ids[1]


@pytest.mark.parametrize("fault", ["duplicate", "missing", "hash", "measurement", "nan"])
def test_ranking_rejects_invalid_search(fault):
    _, _, _, barrier, _, _, comparison, _ = _holdout_fixture()
    search = selection_input(barrier)
    with pytest.raises((ValueError, ValidationError)):
        if fault == "duplicate":
            search = search.model_copy(update={"scores": (search.scores[0],) * 2})
        elif fault == "missing":
            search = search.model_copy(update={"scores": search.scores[:1]})
        elif fault == "hash":
            search = search.model_copy(update={"selection_rule_hash": "sha256:" + "0" * 64})
        elif fault == "measurement":
            search = search.model_copy(
                update={
                    "scores": (
                        search.scores[0].model_copy(
                            update={
                                "round_measurement_ref_id": search.scores[
                                    1
                                ].round_measurement_ref_id,
                            }
                        ),
                        search.scores[1],
                    )
                }
            )
        else:
            SearchSelectionScore(**{**search.scores[0].model_dump(), "mean_effect": float("nan")})
        select_endpoint_candidate(search, barrier, comparison)


def test_finalizer_binds_no_winner_selection_into_evidence():
    store, context, authority, barrier, reveal, holdout, comparison, budget = _holdout_fixture()
    committed = store.publish(selection_input(barrier))
    barrier = barrier.model_copy(update={"input_summary_hash": committed.sha256})
    args = dict(
        context=context,
        round_authority=authority,
        search_barrier=barrier,
        budget_ledger_hash=budget,
        holdout_reveal=reveal,
        holdout_barrier=holdout,
        multiple_comparison=comparison,
    )
    index = _publish_index(store, context, formal_round_evidence_requirements(**args))
    bundle = build_formal_round_evidence(
        **args,
        evidence_index_uri=index.uri,
        evidence_index_hash=index.sha256,
        evidence_reader=store,
    )
    assert bundle.summary["endpoint_selection"] == {
        "rule": "frozen_search_rank_after_batch_d_v1",
        "search_input_hash": committed.sha256,
        "selected_candidate_id": None,
        "status": "not_applicable",
    }
