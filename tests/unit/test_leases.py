import unittest

from dcuopt.domain.enums import LeaseState
from dcuopt.domain.errors import StaleFencingToken
from dcuopt.domain.models import ResourceLease
from dcuopt.leases import (
    acquire,
    begin_release,
    complete_health_check,
    finish_cleanup,
    mark_expired,
    start_fencing,
)


class LeaseTests(unittest.TestCase):
    def test_normal_release_still_fences_before_available(self) -> None:
        lease = ResourceLease(resource_id="dcu-node-1")
        token = acquire(lease, "worker-1")
        begin_release(lease, token)
        start_fencing(lease)
        finish_cleanup(lease)
        complete_health_check(lease, healthy=True)
        self.assertEqual(lease.state, LeaseState.AVAILABLE)
        self.assertIsNone(lease.owner_id)

    def test_expired_lease_cannot_jump_to_available(self) -> None:
        lease = ResourceLease(resource_id="dcu-node-1")
        acquire(lease, "worker-1")
        mark_expired(lease)
        self.assertEqual(lease.state, LeaseState.EXPIRED)

    def test_old_worker_token_is_rejected_after_reacquire(self) -> None:
        lease = ResourceLease(resource_id="dcu-node-1")
        old_token = acquire(lease, "worker-1")
        begin_release(lease, old_token)
        start_fencing(lease)
        finish_cleanup(lease)
        complete_health_check(lease, healthy=True)
        new_token = acquire(lease, "worker-2")
        self.assertGreater(new_token, old_token)
        with self.assertRaises(StaleFencingToken):
            lease.assert_token(old_token)


if __name__ == "__main__":
    unittest.main()

