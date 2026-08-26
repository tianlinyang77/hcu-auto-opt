try:
    from enum import StrEnum
except ImportError:  # Python 3.10 compatibility for the SGLang target image.
    from enum import Enum

    class StrEnum(str, Enum):
        def __str__(self) -> str:
            return self.value


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


class WorkflowType(StrEnum):
    OPTIMIZATION = "optimization"
    FRAMEWORK_SMOKE = "framework_smoke"
    STAGE0 = "stage0"
    MANUAL_CANDIDATE = "manual_candidate"
    SEARCH_ROUND = "search_round"


class FrameworkSmokeDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ManualCandidateDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ManualCandidateKind(StrEnum):
    FIXTURE = "fixture"
    BUSINESS = "business"


class ManualCandidateVerdict(StrEnum):
    FASTER = "faster"
    SLOWER = "slower"
    INCONCLUSIVE = "inconclusive"
    INVALID = "invalid"


class Stage0RunMode(StrEnum):
    DRY_RUN = "dry_run"
    FORMAL = "formal"


class Stage0RunState(StrEnum):
    COLLECTING = "collecting"
    READY = "ready"
    FINALIZED = "finalized"
    FAILED = "failed"


class Stage0ProbeType(StrEnum):
    FINGERPRINT = "fingerprint"
    TIMER = "timer"
    NOISE = "noise"
    KNOWN_SIGNAL = "known_signal"
    NULL_SIGNAL = "null_signal"
    PROFILER = "profiler"
    HOTPATCH = "hotpatch"


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
    FRAMEWORK_SMOKE_PENDING = "framework_smoke_pending"
    SOURCE_PREPARING = "source_preparing"
    ARTIFACT_PREPARING = "artifact_preparing"
    FRAMEWORK_EXECUTING = "framework_executing"
    OUTPUT_VALIDATING = "output_validating"
    FRAMEWORK_RETESTING = "framework_retesting"
    MANUAL_CANDIDATE_PENDING = "manual_candidate_pending"
    MANUAL_BUILDING = "manual_building"
    MANUAL_CORRECTNESS = "manual_correctness"
    MANUAL_PERFORMANCE = "manual_performance"
    MANUAL_ADJUDICATING = "manual_adjudicating"
    CANCELLED = "cancelled"


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
    FRAMEWORK_SMOKE_RUNNING = "framework_smoke_running"
    FRAMEWORK_SMOKE_PASSED = "framework_smoke_passed"
    ADJUDICATING = "adjudicating"
    AWAITING_SIGNOFF = "awaiting_signoff"
    ACCEPTED = "accepted"


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
    EVALUATION = "evaluation"


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
    SOURCE_PREPARE = "source_prepare"
    NOOP_BUILD = "noop_build"
    FRAMEWORK_SMOKE = "framework_smoke"
    STAGE0_PROBE = "stage0_probe"
    MANUAL_BUILD = "manual_build"
    MANUAL_CORRECTNESS = "manual_correctness"
    MANUAL_PERFORMANCE = "manual_performance"
    MANUAL_ADJUDICATE = "manual_adjudicate"


class EvaluationPhase(StrEnum):
    CORRECTNESS = "correctness"
    PERFORMANCE = "performance"
    E2E = "e2e"


class LeaseScope(StrEnum):
    NONE = "none"
    SHARED = "shared"
    EXCLUSIVE = "exclusive"


class SearchRoundRunMode(StrEnum):
    SCRIPTED = "scripted"
    FORMAL = "formal"


class SearchRoundState(StrEnum):
    INTAKE_OPEN = "intake_open"
    INTAKE_CLOSED = "intake_closed"
    BUILDING = "building"
    CORRECTNESS = "correctness"
    SEARCH_MEASURING = "search_measuring"
    SEARCH_BARRIER = "search_barrier"
    HOLDOUT_MEASURING = "holdout_measuring"
    HOLDOUT_BARRIER = "holdout_barrier"
    AWAITING_SIGNOFF = "awaiting_signoff"
    SCRIPTED_COMPLETED = "scripted_completed"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class RoundCandidateState(StrEnum):
    INTAKE_ACCEPTED = "intake_accepted"
    BUILDING = "building"
    BUILD_FAILED = "build_failed"
    BUILT = "built"
    CORRECTNESS_FAILED = "correctness_failed"
    CORRECTNESS_PASSED = "correctness_passed"
    SEARCH_FAILED = "search_failed"
    SEARCH_MEASURED = "search_measured"
    NOT_PROMOTED = "not_promoted"
    HOLDOUT_FAILED = "holdout_failed"
    HOLDOUT_MEASURED = "holdout_measured"
    INVALID = "invalid"


class RoundPhase(StrEnum):
    SEARCH = "search"
    HOLDOUT = "holdout"


class RoundBarrierOutcome(StrEnum):
    MEMBERS_PROMOTED = "members_promoted"
    NO_PROMOTABLE_CANDIDATE = "no_promotable_candidate"
    COMPLETED = "completed"


class RoundTerminalReason(StrEnum):
    HOLDOUT_COMPLETED = "holdout_completed"
    NO_PROMOTABLE_CANDIDATE = "no_promotable_candidate"


class RoundBudgetReservationState(StrEnum):
    RESERVED = "reserved"
    SETTLED = "settled"
    RELEASED = "released"


class RoundBudgetEntryType(StrEnum):
    RESERVE = "reserve"
    SETTLE = "settle"
    RELEASE = "release"
