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


class TargetNotReady(ContractError):
    """A real workflow requested a target whose declared blockers are still open."""


class SourceArtifactError(ContractError):
    """The pinned-source or immutable-artifact pipeline could not proceed safely."""


class SourceIntegrityError(SourceArtifactError):
    """A source checkout no longer matches its recorded snapshot."""


class ArtifactIntegrityError(SourceArtifactError):
    """An artifact does not match its recorded content hash."""


class ExecutionSafetyError(ContractError):
    """A real execution request cannot be launched without violating a safety rule."""


class ImageIdentityError(ExecutionSafetyError):
    """The local container image does not match the locked digest and image ID."""


class ResourceCleanupError(ExecutionSafetyError):
    """An owned process or container could not be fenced and cleaned safely."""
