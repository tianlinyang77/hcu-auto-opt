class ContractError(ValueError):
    """A cross-module domain contract was violated."""


class InvalidTransition(ContractError):
    """A state-machine transition is not allowed."""


class StaleFencingToken(ContractError):
    """A worker attempted to act with an obsolete fencing token."""

