# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Reconstruct the admitted compiler from bounded administrator-owned input."""

from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hcuopt.adapters.business_candidate_family import business_candidate_source_family_hash
from hcuopt.contracts.formal_profile_authorization_v1 import FormalProfileWindowAuthorization
from hcuopt.contracts.formal_readiness_v1 import FormalReadinessManifest, FormalReadinessReport
from hcuopt.contracts.m2_candidate_family_v1 import BusinessCandidateFamilyManifest
from hcuopt.contracts.operator_v1 import OperatorProfileDescriptor
from hcuopt.contracts.platform_v1 import GIT_COMMIT_PATTERN
from hcuopt.deployment.formal_trust import FormalPublicTrust
from hcuopt.domain.errors import NotFound
from hcuopt.measurement.m2_formal_receipt import _read_regular
from hcuopt.operator.formal_plans import FormalOperatorPlanCompiler
from hcuopt.operator.formal_profiles import build_formal_operator_profile_catalog
from hcuopt.operator.profiles import build_operator_service_identity


class FormalCompilerConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["formal-compiler-configuration-v1"]
    source_commit: str = Field(pattern=GIT_COMMIT_PATTERN)
    server_instance_id: UUID
    profiles: tuple[OperatorProfileDescriptor, ...] = Field(min_length=3, max_length=3)
    authorization: FormalProfileWindowAuthorization
    readiness_manifest: FormalReadinessManifest
    readiness_report: FormalReadinessReport
    candidate_family: BusinessCandidateFamilyManifest


class _FrozenFamily:
    def __init__(self, value: BusinessCandidateFamilyManifest):
        self._json = value.model_dump_json()

    def read_manifest(self, *, source_family_hash: str) -> BusinessCandidateFamilyManifest:
        value = BusinessCandidateFamilyManifest.model_validate_json(self._json)
        if business_candidate_source_family_hash(value) != source_family_hash:
            raise NotFound("Formal source family is not in the deployment snapshot")
        return value


def load_formal_compiler(
    *, deployment_root: Path, path: Path, trust: FormalPublicTrust,
    candidate_family_verifier, expected_source_commit: str, clock=None,
) -> FormalOperatorPlanCompiler:
    """No private catalog factory or invented readiness; use original admission.

    The launcher supplies its independently pinned release commit and the real
    package verifier. Config is not proof of the running executable's identity.
    The existing verifier re-reads candidate package bytes during compilation.
    """
    try:
        config = FormalCompilerConfiguration.model_validate_json(
            _read_regular(deployment_root, path, 2 * 1024 * 1024),
        )
    except Exception:
        raise ValueError("Formal compiler configuration is invalid or unavailable") from None
    if config.source_commit != expected_source_commit:
        raise ValueError("Formal compiler release commit differs from deployment")
    if business_candidate_source_family_hash(config.candidate_family) != (
        config.authorization.source_family_hash
    ):
        raise ValueError("Formal compiler source family differs from owner authorization")
    catalog = build_formal_operator_profile_catalog(
        config.profiles, authorization=config.authorization,
        readiness_manifest=config.readiness_manifest, readiness_report=config.readiness_report,
        verifier=trust.owner, clock=clock,
    )
    identity = build_operator_service_identity(
        source_commit=config.source_commit, server_instance_id=config.server_instance_id,
        catalog=catalog,
    )
    return FormalOperatorPlanCompiler(
        catalog, identity, authorization=config.authorization,
        candidate_family_manifest_store=_FrozenFamily(config.candidate_family),
        candidate_family_verifier=candidate_family_verifier, clock=clock,
    )
