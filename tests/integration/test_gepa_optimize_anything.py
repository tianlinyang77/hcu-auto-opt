# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from uuid import UUID

import pytest

gepa = pytest.importorskip("gepa.optimize_anything")

from hcuopt.agent.gepa_demo import (  # noqa: E402
    GepaCaseEvaluation,
    GepaScriptedCase,
    run_gepa_scripted_demo,
)
from hcuopt.contracts.agent_v1 import GenerationBudget  # noqa: E402
from hcuopt.contracts.agent_verification_v1 import (  # noqa: E402
    AgentBudgetUsage,
    AgentGenerationReadModel,
    AgentProposalReadModel,
)


def _read_model(run_id: int) -> AgentGenerationReadModel:
    return AgentGenerationReadModel.model_construct(
        generated_at=None,
        status="ready_for_review",
        generation_run_id=UUID(int=run_id),
        request_id=UUID(int=2),
        plan_id=UUID(int=3),
        target_id="gepa-scripted-target",
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
            output_bytes=100,
            output_tokens=10,
            wall_seconds=0.01,
            proposal_count=1,
        ),
        attempts=(),
        proposals=(
            AgentProposalReadModel.model_construct(
                proposal_id=UUID(int=5), status="kept", reason_code=None
            ),
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


def test_real_gepa_engine_uses_bounded_d_verified_scripted_evaluations() -> None:
    counter = 100

    def case_evaluator(_policy: str, _case: GepaScriptedCase) -> GepaCaseEvaluation:
        nonlocal counter
        counter += 1
        model = _read_model(counter)
        return GepaCaseEvaluation(model, model.knowledge_hash)

    def scripted_proposer(candidate, _reflective_dataset, components_to_update):
        return {
            component: f"{candidate[component]}\nKeep changes small and testable."
            for component in components_to_update
        }

    config = gepa.GEPAConfig(
        engine=gepa.EngineConfig(max_metric_calls=3),
        reflection=gepa.ReflectionConfig(custom_candidate_proposer=scripted_proposer),
    )
    result = run_gepa_scripted_demo(
        seed_policy="Generate a bounded source-only Proposal.",
        cases=(
            GepaScriptedCase(
                case_id="fixed-scripted-case",
                target_id="gepa-scripted-target",
                baseline_epoch_id=str(UUID(int=4)),
                replacement_point="module.kernel",
            ),
        ),
        case_evaluator=case_evaluator,
        optimize_anything=gepa.optimize_anything,
        config=config,
        max_metric_calls=3,
    )

    assert 1 <= result.metric_calls <= 3
    assert result.generation_runs == result.metric_calls
    assert result.seed_policy_hash.startswith("sha256:")
    assert len(result.evaluations) == result.metric_calls
    assert all(item["hcu_accessed"] is False for item in result.evaluations)
    assert all(item["performance_conclusion"] == "not_measured" for item in result.evaluations)
