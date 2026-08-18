class ContractError(ValueError):
    """A cross-module domain contract was violated."""


class InvalidTransition(ContractError):
    """A state-machine transition is not allowed."""


class StaleFencingToken(ContractError):
    """A worker attempted to act with an obsolete fencing token."""


class NotFound(ContractError):
    """A requested domain object does not exist."""


class Conflict(ContractError):
    """The request conflicts with persisted state or an idempotency key."""


class StaleClaimToken(ContractError):
    """A worker attempted to finish a job it no longer owns."""


class TargetConfigError(ContractError):
    """A target lock file is missing, malformed, or violates the platform contract."""


class AdapterUnavailable(ContractError):
    """A workflow requested an adapter that is not registered in this worker."""
