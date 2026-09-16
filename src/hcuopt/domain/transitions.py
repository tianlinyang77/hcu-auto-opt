from collections.abc import Mapping
from typing import TypeVar

from hcuopt.domain.enums import CandidateState, LeaseState, TaskState
from hcuopt.domain.errors import InvalidTransition

StateT = TypeVar("StateT", TaskState, CandidateState, LeaseState)


TASK_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {
    TaskState.CREATED: frozenset(
        {TaskState.STAGE0_PENDING, TaskState.FRAMEWORK_SMOKE_PENDING}
    ),
    TaskState.STAGE0_PENDING: frozenset(
        {
            TaskState.STOPPED_MEASUREMENT,
            TaskState.DEGRADED,
            TaskState.BASELINE_PENDING,
            TaskState.CANCELLED,
        }
    ),
    TaskState.DEGRADED: frozenset({TaskState.BASELINE_PENDING}),
    TaskState.BASELINE_PENDING: frozenset(
        {TaskState.PROFILING, TaskState.MANUAL_CANDIDATE_PENDING}
    ),
    TaskState.PROFILING: frozenset({TaskState.SEARCHING}),
    TaskState.SEARCHING: frozenset({TaskState.EVALUATING}),
    TaskState.EVALUATING: frozenset({TaskState.MODEL_VALIDATING}),
    TaskState.MODEL_VALIDATING: frozenset({TaskState.E2E_VALIDATING, TaskState.REJECTED}),
    TaskState.E2E_VALIDATING: frozenset({TaskState.AWAITING_SIGNOFF, TaskState.REJECTED}),
    TaskState.AWAITING_SIGNOFF: frozenset(
        {
            TaskState.COMPLETED,
            TaskState.REJECTED,
            TaskState.FRAMEWORK_RETESTING,
            TaskState.CANCELLED,
        }
    ),
    TaskState.FRAMEWORK_SMOKE_PENDING: frozenset(
        {TaskState.SOURCE_PREPARING, TaskState.REJECTED, TaskState.CANCELLED}
    ),
    TaskState.SOURCE_PREPARING: frozenset(
        {TaskState.ARTIFACT_PREPARING, TaskState.REJECTED, TaskState.CANCELLED}
    ),
    TaskState.ARTIFACT_PREPARING: frozenset(
        {TaskState.FRAMEWORK_EXECUTING, TaskState.REJECTED, TaskState.CANCELLED}
    ),
    TaskState.FRAMEWORK_EXECUTING: frozenset(
        {TaskState.OUTPUT_VALIDATING, TaskState.REJECTED, TaskState.CANCELLED}
    ),
    TaskState.OUTPUT_VALIDATING: frozenset(
        {TaskState.AWAITING_SIGNOFF, TaskState.REJECTED, TaskState.CANCELLED}
    ),
    TaskState.FRAMEWORK_RETESTING: frozenset(
        {TaskState.AWAITING_SIGNOFF, TaskState.REJECTED, TaskState.CANCELLED}
    ),
    TaskState.MANUAL_CANDIDATE_PENDING: frozenset(
        {TaskState.MANUAL_BUILDING, TaskState.CANCELLED}
    ),
    TaskState.MANUAL_BUILDING: frozenset(
        {TaskState.MANUAL_CORRECTNESS, TaskState.REJECTED, TaskState.CANCELLED}
    ),
    TaskState.MANUAL_CORRECTNESS: frozenset(
        {TaskState.MANUAL_PERFORMANCE, TaskState.REJECTED, TaskState.CANCELLED}
    ),
    TaskState.MANUAL_PERFORMANCE: frozenset(
        {TaskState.MANUAL_ADJUDICATING, TaskState.REJECTED, TaskState.CANCELLED}
    ),
    TaskState.MANUAL_ADJUDICATING: frozenset(
        {TaskState.AWAITING_SIGNOFF, TaskState.REJECTED, TaskState.CANCELLED}
    ),
    TaskState.ENDPOINT_PROBING: frozenset(
        {TaskState.ENDPOINT_PROVISIONAL_PASSED, TaskState.REJECTED, TaskState.CANCELLED}
    ),
}

CANDIDATE_TRANSITIONS: Mapping[CandidateState, frozenset[CandidateState]] = {
    CandidateState.PROPOSED: frozenset({CandidateState.BUILDING}),
    CandidateState.BUILDING: frozenset({CandidateState.BUILT, CandidateState.BUILD_FAILED}),
    CandidateState.BUILT: frozenset(
        {CandidateState.CORRECTNESS_RUNNING, CandidateState.FRAMEWORK_SMOKE_RUNNING}
    ),
    CandidateState.CORRECTNESS_RUNNING: frozenset(
        {CandidateState.PERFORMANCE_RUNNING, CandidateState.REJECTED}
    ),
    CandidateState.PERFORMANCE_RUNNING: frozenset(
        {
            CandidateState.ROUND_WAITING,
            CandidateState.ADJUDICATING,
            CandidateState.REJECTED,
        }
    ),
    CandidateState.ROUND_WAITING: frozenset(
        {CandidateState.MODEL_VALIDATING, CandidateState.REJECTED}
    ),
    CandidateState.MODEL_VALIDATING: frozenset({CandidateState.STAGED, CandidateState.REJECTED}),
    CandidateState.STAGED: frozenset({CandidateState.E2E_RUNNING}),
    CandidateState.E2E_RUNNING: frozenset(
        {CandidateState.RELEASE_CANDIDATE, CandidateState.REJECTED}
    ),
    CandidateState.FRAMEWORK_SMOKE_RUNNING: frozenset(
        {CandidateState.FRAMEWORK_SMOKE_PASSED, CandidateState.REJECTED}
    ),
    CandidateState.FRAMEWORK_SMOKE_PASSED: frozenset(
        {CandidateState.FRAMEWORK_SMOKE_RUNNING, CandidateState.REJECTED}
    ),
    CandidateState.ADJUDICATING: frozenset(
        {CandidateState.AWAITING_SIGNOFF, CandidateState.REJECTED}
    ),
    CandidateState.AWAITING_SIGNOFF: frozenset(
        {CandidateState.ACCEPTED, CandidateState.REJECTED}
    ),
}

LEASE_TRANSITIONS: Mapping[LeaseState, frozenset[LeaseState]] = {
    LeaseState.AVAILABLE: frozenset({LeaseState.ACTIVE}),
    LeaseState.ACTIVE: frozenset(
        {LeaseState.RELEASING, LeaseState.EXPIRED, LeaseState.WORKER_LOST}
    ),
    LeaseState.RELEASING: frozenset({LeaseState.FENCING}),
    LeaseState.EXPIRED: frozenset({LeaseState.FENCING}),
    LeaseState.WORKER_LOST: frozenset({LeaseState.FENCING}),
    LeaseState.FENCING: frozenset({LeaseState.HEALTH_CHECK}),
    LeaseState.HEALTH_CHECK: frozenset({LeaseState.AVAILABLE, LeaseState.QUARANTINED}),
}


def transition(
    current: StateT,
    target: StateT,
    graph: Mapping[StateT, frozenset[StateT]],
) -> StateT:
    if target not in graph.get(current, frozenset()):
        raise InvalidTransition(f"invalid transition: {current.value} -> {target.value}")
    return target


def transition_task(current: TaskState, target: TaskState) -> TaskState:
    return transition(current, target, TASK_TRANSITIONS)


def transition_candidate(current: CandidateState, target: CandidateState) -> CandidateState:
    return transition(current, target, CANDIDATE_TRANSITIONS)


def transition_lease(current: LeaseState, target: LeaseState) -> LeaseState:
    return transition(current, target, LEASE_TRANSITIONS)
