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
