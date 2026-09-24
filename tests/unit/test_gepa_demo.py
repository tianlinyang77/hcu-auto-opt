# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from uuid import UUID

import pytest

from hcuopt.agent.gepa_demo import (
    GepaCaseEvaluation,
    GepaDemoError,
    GepaScriptedCase,
    run_gepa_scripted_demo,
    score_verified_generation,
)
from hcuopt.contracts.agent_v1 import GenerationBudget
from hcuopt.contracts.agent_verification_v1 import (
    AgentBudgetUsage,
    AgentGenerationReadModel,
    AgentProposalReadModel,
)


def _read_model(*, status: str = "ready_for_review") -> AgentGenerationReadModel:
    return AgentGenerationReadModel.model_construct(
        generated_at=None,
        status=status,
        generation_run_id=UUID(int=1),
        request_id=UUID(int=2),
        plan_id=UUID(int=3),
        target_id="scripted-target",
        baseline_epoch_id=UUID(int=4),
        replacement_point="module.kernel",
        input_digest="sha256:" + "a" * 64,
        knowledge_hash="sha256:" + "b" * 64,
        request_hash="sha256:" + "c" * 64,
        plan_hash="sha256:" + "d" * 64,
        budget_limit=GenerationBudget(
            max_generator_attempts=1,
            max_wall_seconds=10,
            max_total_output_bytes=1000,
            max_total_tokens=100,
            max_proposals=2,
        ),
        budget=AgentBudgetUsage(
            attempt_count=1,
            wall_seconds=0.01,
            output_bytes=100,
            output_tokens=10,
            proposal_count=2,
        ),
        attempts=(),
        proposals=(
            AgentProposalReadModel.model_construct(
                proposal_id=UUID(int=5), status="kept", reason_code=None
            ),
            AgentProposalReadModel.model_construct(status="eliminated", reason_code="duplicate"),
        ),
        failure_codes=(),
        human_review_status="pending",
        package_promotion_status="pending",
        formal_readiness="hold",
        adapter_provenance=(),
        synthetic=True,
        environment="scripted_dev_only",
        performance_conclusion="not_measured",
        formal_intake_allowed=False,
        automatic_release_allowed=False,
    )


def test_score_uses_only_d_verified_kept_proposals() -> None:
    assert score_verified_generation(_read_model()) == 0.5
    assert score_verified_generation(_read_model(status="invalid")) == 0.0


def test_gepa_bridge_evaluates_fixed_cases_and_returns_redacted_feedback() -> None:
    case = GepaScriptedCase(
        case_id="fixture-1",
        target_id="scripted-target",
        baseline_epoch_id=str(UUID(int=4)),
        replacement_point="module.kernel",
    )
    seen: list[tuple[str, str]] = []
    captured: dict[str, object] = {}

    def evaluate(policy: str, selected_case: GepaScriptedCase) -> GepaCaseEvaluation:
        seen.append((policy, selected_case.case_id))
        model = _read_model()
        return GepaCaseEvaluation(model, model.knowledge_hash)

    def optimize_anything(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        score, feedback = kwargs["evaluator"]("candidate policy")
        assert score == 0.5
        assert feedback["performance_conclusion"] == "not_measured"
        assert feedback["verified_scripted_episodes"][0]["case_id"] == "fixture-1"
        assert "patch_preview" not in str(feedback)
        return {"best_candidate": "candidate policy"}

    result = run_gepa_scripted_demo(
        seed_policy="seed policy",
        cases=(case,),
        case_evaluator=evaluate,
        optimize_anything=optimize_anything,
        config=object(),
        max_metric_calls=2,
    )

    assert result.metric_calls == 1
    assert result.generation_runs == 1
    assert result.optimizer_result == {"best_candidate": "candidate policy"}
    assert seen == [("candidate policy", "fixture-1")]
    assert captured["seed_candidate"] == "seed policy"


def test_gepa_bridge_rejects_cross_case_or_changed_policy_evidence() -> None:
    case = GepaScriptedCase(
        case_id="fixture-1",
        target_id="scripted-target",
        baseline_epoch_id=str(UUID(int=4)),
        replacement_point="module.kernel",
    )

    def optimize_anything(**kwargs):  # type: ignore[no-untyped-def]
        kwargs["evaluator"]("policy")

    with pytest.raises(GepaDemoError, match="does not bind"):
        run_gepa_scripted_demo(
            seed_policy="seed policy",
            cases=(case,),
            case_evaluator=lambda _policy, _case: GepaCaseEvaluation(
                _read_model(), "sha256:" + "e" * 64
            ),
            optimize_anything=optimize_anything,
            config=object(),
        )


def test_gepa_bridge_bounds_metric_calls_and_generation_runs() -> None:
    with pytest.raises(GepaDemoError, match="Generation Run count"):
        run_gepa_scripted_demo(
            seed_policy="seed policy",
            cases=(
                GepaScriptedCase("one", "t", "b", "m"),
                GepaScriptedCase("two", "t", "b", "m"),
                GepaScriptedCase("three", "t", "b", "m"),
            ),
            case_evaluator=lambda _policy, _case: GepaCaseEvaluation(
                _read_model(), _read_model().knowledge_hash
            ),
            optimize_anything=lambda **_kwargs: None,
            config=object(),
            max_metric_calls=8,
        )
