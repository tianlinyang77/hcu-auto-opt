from dcuopt.domain.enums import LeaseState
from dcuopt.domain.models import ResourceLease
from dcuopt.domain.transitions import transition_lease


def acquire(lease: ResourceLease, owner_id: str) -> int:
    lease.state = transition_lease(lease.state, LeaseState.ACTIVE)
    lease.fencing_token += 1
    lease.owner_id = owner_id
    return lease.fencing_token


def begin_release(lease: ResourceLease, token: int) -> None:
    lease.assert_token(token)
    lease.state = transition_lease(lease.state, LeaseState.RELEASING)


def mark_expired(lease: ResourceLease) -> None:
    lease.state = transition_lease(lease.state, LeaseState.EXPIRED)


def start_fencing(lease: ResourceLease) -> None:
    lease.state = transition_lease(lease.state, LeaseState.FENCING)


def finish_cleanup(lease: ResourceLease) -> None:
    lease.state = transition_lease(lease.state, LeaseState.HEALTH_CHECK)


def complete_health_check(lease: ResourceLease, healthy: bool) -> None:
    target = LeaseState.AVAILABLE if healthy else LeaseState.QUARANTINED
    lease.state = transition_lease(lease.state, target)
    if healthy:
        lease.owner_id = None

