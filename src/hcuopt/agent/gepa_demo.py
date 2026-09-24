# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Opt-in GEPA bridge for bounded, scripted M2b proposal strategy experiments.

The bridge deliberately accepts an evaluator that runs the existing M2b flow and
returns D's durable read model. It does not execute an Agent, create a Candidate,
or access an HCU itself.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Protocol

from hcuopt.contracts.agent_verification_v1 import AgentGenerationReadModel
from hcuopt.measurement.evidence import EvidenceArtifact, write_evidence

MAX_GEPA_METRIC_CALLS = 8
MAX_SCRIPTED_CASES = 4
MAX_GENERATION_RUNS = 16


class GepaDemoError(ValueError):
    """A GEPA experiment is outside the bounded Scripted demo contract."""


@dataclass(frozen=True, slots=True)
class GepaScriptedCase:
    """One fixed Scripted task and its immutable target binding."""

    case_id: str
    target_id: str
    baseline_epoch_id: str
    replacement_point: str

    def __post_init__(self) -> None:
        for name, value in (
            ("case_id", self.case_id),
            ("target_id", self.target_id),
            ("baseline_epoch_id", self.baseline_epoch_id),
            ("replacement_point", self.replacement_point),
        ):
            if not value or value.strip() != value:
                raise GepaDemoError(f"{name} must be non-empty normalized text")


@dataclass(frozen=True, slots=True)
class GepaCaseEvaluation:
    """A D-read result plus the independently computed policy snapshot Hash."""

    read_model: AgentGenerationReadModel
    expected_knowledge_hash: str


class GepaCaseEvaluator(Protocol):
    """Run one policy/case through existing Agent/Apex and D evidence services."""

    def __call__(self, policy: str, case: GepaScriptedCase) -> GepaCaseEvaluation: ...


class OptimizeAnything(Protocol):
    """The subset of GEPA's public optimize_anything function used here."""

    def __call__(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class GepaScriptedDemoResult:
    optimizer_result: Any
    seed_policy_hash: str
    metric_calls: int
    generation_runs: int
    max_metric_calls: int
    evaluations: tuple[Mapping[str, Any], ...]


def score_verified_generation(read_model: AgentGenerationReadModel) -> float:
    """Score only D's verified Scripted output yield; never infer performance."""

    if (
        read_model.synthetic is not True
        or read_model.environment != "scripted_dev_only"
        or read_model.performance_conclusion != "not_measured"
        or read_model.formal_intake_allowed is not False
        or read_model.automatic_release_allowed is not False
        or read_model.formal_readiness != "hold"
        or read_model.status != "ready_for_review"
    ):
        return 0.0
    maximum = read_model.budget_limit.max_proposals
    if maximum < 1:
        return 0.0
    kept = sum(proposal.status == "kept" for proposal in read_model.proposals)
    return min(1.0, kept / maximum)


def _feedback(
    case: GepaScriptedCase,
    evaluation: GepaCaseEvaluation,
) -> dict[str, Any]:
    model = evaluation.read_model
    return {
        "case_id": case.case_id,
        "generation_run_id": str(model.generation_run_id),
        "status": model.status,
        "failure_codes": list(model.failure_codes),
        "kept_proposals": sum(item.status == "kept" for item in model.proposals),
        "kept_proposal_ids": [
            str(item.proposal_id) for item in model.proposals if item.status == "kept"
        ],
        "eliminated_proposals": sum(
            item.status == "eliminated" for item in model.proposals
        ),
        "proposal_reasons": [
            {"status": item.status, "reason_code": item.reason_code}
            for item in model.proposals
        ],
        "attempt_failures": [
            {"status": item.status, "reason_code": item.reason_code}
            for item in model.attempts
            if item.status != "succeeded"
        ],
        "budget_used": {
            "attempts": model.budget.attempt_count,
            "tokens": model.budget.output_tokens,
            "output_bytes": model.budget.output_bytes,
            "wall_seconds": model.budget.wall_seconds,
            "proposals": model.budget.proposal_count,
        },
        "input_digest": model.input_digest,
        "knowledge_hash": model.knowledge_hash,
        "request_hash": model.request_hash,
        "plan_hash": model.plan_hash,
        "performance_conclusion": "not_measured",
    }


def run_gepa_scripted_demo(
    *,
    seed_policy: str,
    cases: Sequence[GepaScriptedCase],
    case_evaluator: GepaCaseEvaluator,
    optimize_anything: OptimizeAnything,
    config: Any,
    max_metric_calls: int = 4,
) -> GepaScriptedDemoResult:
    """Evolve an advisory generation policy using verified Scripted episodes.

    ``case_evaluator`` owns the adapter to the existing Agent/Apex, package,
    family and Scripted Intake interfaces. It must publish a new immutable
    KnowledgeSnapshot for each policy and return D's read model for that Run.
    Human approval/promotion of a selected result remains a separate operation.
    """

    if not isinstance(seed_policy, str) or not seed_policy.strip():
        raise GepaDemoError("seed policy must be non-empty text")
    if not isinstance(max_metric_calls, int) or isinstance(max_metric_calls, bool):
        raise GepaDemoError("max_metric_calls must be an integer")
    if not 1 <= max_metric_calls <= MAX_GEPA_METRIC_CALLS:
        raise GepaDemoError("max_metric_calls exceeds the Scripted demo limit")
    fixed_cases = tuple(cases)
    if not fixed_cases or len(fixed_cases) > MAX_SCRIPTED_CASES:
        raise GepaDemoError("Scripted demo requires one to four fixed cases")
    if len({case.case_id for case in fixed_cases}) != len(fixed_cases):
        raise GepaDemoError("Scripted demo case IDs must be unique")
    if max_metric_calls * len(fixed_cases) > MAX_GENERATION_RUNS:
        raise GepaDemoError("GEPA budget could exceed the bounded Generation Run count")

    metric_calls = 0
    metric_calls_lock = Lock()
    evaluation_records: list[Mapping[str, Any]] = []
    used_generation_run_ids: set[str] = set()

    def evaluate(policy: str) -> tuple[float, Mapping[str, Any]]:
        nonlocal metric_calls
        with metric_calls_lock:
            metric_calls += 1
            call_number = metric_calls
            if call_number > max_metric_calls:
                raise GepaDemoError("GEPA exceeded the configured metric-call budget")
        if not isinstance(policy, str) or not policy.strip():
            return 0.0, {"failure": "empty_policy"}
        policy_hash = "sha256:" + hashlib.sha256(policy.encode("utf-8")).hexdigest()

        observations: list[dict[str, Any]] = []
        scores: list[float] = []
        for case in fixed_cases:
            result = case_evaluator(policy, case)
            model = result.read_model
            if (
                model.target_id != case.target_id
                or str(model.baseline_epoch_id) != case.baseline_epoch_id
                or model.replacement_point != case.replacement_point
                or model.knowledge_hash != result.expected_knowledge_hash
                or not _is_sha256(model.input_digest)
                or not _is_sha256(model.knowledge_hash)
                or not _is_sha256(result.expected_knowledge_hash)
            ):
                raise GepaDemoError("D Read Model does not bind this policy evaluation")
            run_id = str(model.generation_run_id)
            with metric_calls_lock:
                if run_id in used_generation_run_ids:
                    raise GepaDemoError(
                        "each Scripted policy evaluation must use a fresh Generation Run"
                    )
                used_generation_run_ids.add(run_id)
            score = score_verified_generation(model)
            if not math.isfinite(score):
                raise GepaDemoError("verified Scripted score must be finite")
            scores.append(score)
            observations.append(
                {"score": score, "evidence": _feedback(case, result)}
            )

        score = sum(scores) / len(scores)
        feedback = {
            "verified_scripted_episodes": observations,
            "score_definition": "mean D-verified kept-proposal yield; not performance",
            "hcu_accessed": False,
            "holdout_accessed": False,
            "performance_conclusion": "not_measured",
        }
        with metric_calls_lock:
            evaluation_records.append(
                {
                    "call_number": call_number,
                    "policy_hash": policy_hash,
                    "score": score,
                    **feedback,
                }
            )
        return score, feedback

    optimizer_result = optimize_anything(
        seed_candidate=seed_policy,
        evaluator=evaluate,
        objective=(
            "Improve the advisory Agent proposal-generation policy on the fixed "
            "Scripted cases. Maximize D-verified kept Proposal yield while respecting "
            "the existing Request, Plan, Runner, dedupe, and review contracts. "
            "Performance is not measured and no release authority is granted."
        ),
        config=config,
    )
    return GepaScriptedDemoResult(
        optimizer_result=optimizer_result,
        seed_policy_hash="sha256:" + hashlib.sha256(seed_policy.encode("utf-8")).hexdigest(),
        metric_calls=metric_calls,
        generation_runs=metric_calls * len(fixed_cases),
        max_metric_calls=max_metric_calls,
        evaluations=tuple(
            sorted(evaluation_records, key=lambda item: int(item["call_number"]))
        ),
    )


def _is_sha256(value: str) -> bool:
    return (
        len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def write_gepa_demo_evidence(
    path: Path,
    result: GepaScriptedDemoResult,
    *,
    optimizer_identity: str,
) -> EvidenceArtifact:
    """Persist deterministic, immutable GEPA evaluation receipts without policy text."""

    if not optimizer_identity or optimizer_identity.strip() != optimizer_identity:
        raise GepaDemoError("optimizer identity must be non-empty normalized text")
    return write_evidence(
        path,
        {
            "schema_version": "hcuopt-gepa-scripted-evidence-v1",
            "optimizer_identity": optimizer_identity,
            "seed_policy_hash": result.seed_policy_hash,
            "metric_calls": result.metric_calls,
            "max_metric_calls": result.max_metric_calls,
            "generation_runs": result.generation_runs,
            "evaluations": list(result.evaluations),
            "synthetic": True,
            "environment": "scripted_dev_only",
            "performance_conclusion": "not_measured",
            "formal_intake_allowed": False,
            "automatic_release_allowed": False,
        },
    )


__all__ = [
    "GepaCaseEvaluation",
    "GepaCaseEvaluator",
    "GepaDemoError",
    "GepaScriptedCase",
    "GepaScriptedDemoResult",
    "MAX_GEPA_METRIC_CALLS",
    "MAX_GENERATION_RUNS",
    "MAX_SCRIPTED_CASES",
    "OptimizeAnything",
    "run_gepa_scripted_demo",
    "score_verified_generation",
    "write_gepa_demo_evidence",
]
