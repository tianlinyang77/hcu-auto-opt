# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from hcuopt.domain.errors import Conflict, NotFound


class OperatorProfileNotFound(NotFound):
    code = "operator_profile_not_found"
    retryable = False


class OperatorProfileRevoked(Conflict):
    code = "operator_profile_revoked"
    retryable = False


class OperatorProfileModeMismatch(Conflict):
    code = "operator_profile_mode_mismatch"
    retryable = False


class OperatorPlanHashMismatch(Conflict):
    code = "operator_plan_hash_mismatch"
    retryable = False


class OperatorServiceIdentityMismatch(Conflict):
    code = "service_identity_mismatch"
    retryable = False


class OperatorPreviewBlocked(Conflict):
    code = "operator_preview_blocked"
    retryable = True


class OperatorPreviewExpired(Conflict):
    code = "operator_preview_expired"
    retryable = True


class OperatorWarningAcknowledgementRequired(Conflict):
    code = "operator_warning_ack_required"
    retryable = True


class OperatorStartFailed(Conflict):
    code = "operator_start_failed"
    retryable = True
