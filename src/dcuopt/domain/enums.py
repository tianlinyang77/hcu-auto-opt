from enum import StrEnum


class GateResult(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class ProfilerCapability(StrEnum):
    FULL = "full"
    DEGRADED = "degraded"
    NONE = "none"


class HotPatchCapability(StrEnum):
    HOT_PATCH = "hot_patch"
    OVERLAY_ONLY = "overlay_only"
    NONE = "none"


class ProjectMode(StrEnum):
    STOPPED_MEASUREMENT = "stopped_measurement"
    FULL_MVP = "full_mvp"
    DEGRADED_MANUAL_INTAKE = "degraded_manual_intake"
    CONFIG_ONLY = "config_only"


class TaskState(StrEnum):
    CREATED = "created"
    STAGE0_PENDING = "stage0_pending"
    STOPPED_MEASUREMENT = "stopped_measurement"
    DEGRADED = "degraded"
    BASELINE_PENDING = "baseline_pending"
    PROFILING = "profiling"
    SEARCHING = "searching"
    EVALUATING = "evaluating"
    MODEL_VALIDATING = "model_validating"
    E2E_VALIDATING = "e2e_validating"
    AWAITING_SIGNOFF = "awaiting_signoff"
    COMPLETED = "completed"
    REJECTED = "rejected"


class CandidateState(StrEnum):
    PROPOSED = "proposed"
    BUILDING = "building"
    BUILD_FAILED = "build_failed"
    BUILT = "built"
    CORRECTNESS_RUNNING = "correctness_running"
    PERFORMANCE_RUNNING = "performance_running"
    ROUND_WAITING = "round_waiting"
    MODEL_VALIDATING = "model_validating"
    STAGED = "staged"
    E2E_RUNNING = "e2e_running"
    RELEASE_CANDIDATE = "release_candidate"
    REJECTED = "rejected"


class LeaseState(StrEnum):
    AVAILABLE = "available"
    ACTIVE = "active"
    RELEASING = "releasing"
    EXPIRED = "expired"
    WORKER_LOST = "worker_lost"
    FENCING = "fencing"
    HEALTH_CHECK = "health_check"
    QUARANTINED = "quarantined"


class OptimizationTrack(StrEnum):
    CONFIG = "config"
    TRITON = "triton"
    HIP = "hip"


class ReleaseMode(StrEnum):
    HOT_PATCH = "hot_patch"
    OVERLAY = "overlay"
    MANUAL_ONLY = "manual_only"


class WorkerType(StrEnum):
    AGENT = "agent"
    BUILD = "build"
    GPU = "gpu"


class WorkerState(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobType(StrEnum):
    PROFILE = "profile"
    CANDIDATE_GENERATE = "candidate_generate"
    BUILD = "build"
    CORRECTNESS = "correctness"
    PERFORMANCE = "performance"
    E2E = "e2e"


class EvaluationPhase(StrEnum):
    CORRECTNESS = "correctness"
    PERFORMANCE = "performance"
    E2E = "e2e"


class LeaseScope(StrEnum):
    NONE = "none"
    SHARED = "shared"
    EXCLUSIVE = "exclusive"
