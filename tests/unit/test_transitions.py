import unittest

from hcuopt.domain.enums import CandidateState, LeaseState, TaskState
from hcuopt.domain.errors import InvalidTransition
from hcuopt.domain.transitions import (
    transition_candidate,
    transition_lease,
    transition_task,
)


class TransitionTests(unittest.TestCase):
    def test_task_happy_path_begins_with_stage0(self) -> None:
        self.assertEqual(
            transition_task(TaskState.CREATED, TaskState.STAGE0_PENDING),
            TaskState.STAGE0_PENDING,
        )

    def test_task_cannot_skip_stage0(self) -> None:
        with self.assertRaises(InvalidTransition):
            transition_task(TaskState.CREATED, TaskState.SEARCHING)

    def test_candidate_cannot_skip_correctness(self) -> None:
        with self.assertRaises(InvalidTransition):
            transition_candidate(CandidateState.BUILT, CandidateState.PERFORMANCE_RUNNING)

    def test_expired_lease_must_enter_fencing(self) -> None:
        self.assertEqual(
            transition_lease(LeaseState.EXPIRED, LeaseState.FENCING),
            LeaseState.FENCING,
        )
        with self.assertRaises(InvalidTransition):
            transition_lease(LeaseState.EXPIRED, LeaseState.AVAILABLE)


if __name__ == "__main__":
    unittest.main()

