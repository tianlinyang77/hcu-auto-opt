# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import UUID

from hcuopt.contracts.m2 import M2_SEARCH_ROUND_SCHEMA_VERSION, RoundBudget
from hcuopt.contracts.operator_v1 import (
    MeasurementOperatorProfileRefs,
    OperatorProfileContent,
    OperatorProfileDescriptor,
    OperatorProfileRef,
    OperatorServiceIdentity,
    TargetOperatorProfileRefs,
    WorkloadOperatorProfileRefs,
)
from hcuopt.domain.enums import SearchRoundRunMode
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.operator.errors import (
    OperatorPlanHashMismatch,
    OperatorProfileModeMismatch,
    OperatorProfileNotFound,
    OperatorProfileRevoked,
)


def _sha256(value: bytes | str) -> str:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def operator_profile_hash(content: OperatorProfileContent) -> str:
    return _sha256(canonical_json_bytes(content))


def publish_operator_profile(content: OperatorProfileContent) -> OperatorProfileDescriptor:
    return OperatorProfileDescriptor.model_validate(
        {**content.model_dump(mode="json"), "profile_hash": operator_profile_hash(content)}
    )


def profile_catalog_hash(profiles: tuple[OperatorProfileDescriptor, ...]) -> str:
    document = [
        {
            "profile_id": profile.profile_id,
            "profile_version": profile.profile_version,
            "profile_kind": profile.profile_kind,
            "profile_hash": profile.profile_hash,
        }
        for profile in sorted(
            profiles,
            key=lambda item: (item.profile_kind, item.profile_id, item.profile_version),
        )
    ]
    return _sha256(canonical_json_bytes(document))


class OperatorProfileCatalog:
    def __init__(
        self,
        profiles: tuple[OperatorProfileDescriptor, ...],
        *,
        allow_real_profiles: bool = False,
    ) -> None:
        indexed: dict[tuple[str, str, int], OperatorProfileDescriptor] = {}
        for profile in profiles:
            content = OperatorProfileContent.model_validate(
                profile.model_dump(mode="json", exclude={"profile_hash"})
            )
            if operator_profile_hash(content) != profile.profile_hash:
                raise ValueError("Operator Profile content does not match profile_hash")
            if not allow_real_profiles and not profile.synthetic:
                raise ValueError("Real Operator Profiles require separate authorization")
            identity = (profile.profile_kind, profile.profile_id, profile.profile_version)
            if identity in indexed:
                raise ValueError("Operator Profile identity must be unique")
            indexed[identity] = profile
        self._profiles = indexed
        self.catalog_hash = profile_catalog_hash(tuple(indexed.values()))

    def list(
        self, profile_kind: str | None = None
    ) -> list[OperatorProfileDescriptor]:
        selected = [
            profile
            for (kind, _profile_id, _version), profile in self._profiles.items()
            if profile_kind is None or kind == profile_kind
        ]
        return sorted(
            selected,
            key=lambda item: (item.profile_kind, item.profile_id, item.profile_version),
        )

    def get(
        self, profile_kind: str, profile_id: str, profile_version: int
    ) -> OperatorProfileDescriptor:
        try:
            return self._profiles[(profile_kind, profile_id, profile_version)]
        except KeyError as error:
            raise OperatorProfileNotFound(
                "exact Operator Profile version is not registered"
            ) from error

    def require(
        self,
        reference: OperatorProfileRef,
        run_mode: SearchRoundRunMode,
    ) -> OperatorProfileDescriptor:
        identity = (
            reference.profile_kind,
            reference.profile_id,
            reference.profile_version,
        )
        profile = self.get(*identity)
        if profile.profile_hash != reference.profile_hash:
            raise OperatorPlanHashMismatch("Operator Profile Hash changed")
        if profile.state == "revoked":
            raise OperatorProfileRevoked("Operator Profile cannot start new work")
        if run_mode not in profile.allowed_run_modes:
            raise OperatorProfileModeMismatch(
                "Operator Profile does not allow the requested run mode"
            )
        return profile


def build_operator_service_identity(
    *,
    source_commit: str,
    server_instance_id: UUID,
    catalog: OperatorProfileCatalog,
) -> OperatorServiceIdentity:
    return OperatorServiceIdentity(
        source_commit=source_commit,
        control_contract_version=M2_SEARCH_ROUND_SCHEMA_VERSION,
        profile_catalog_hash=catalog.catalog_hash,
        server_instance_id=server_instance_id,
    )


def build_scripted_operator_profile_catalog() -> OperatorProfileCatalog:
    created_at = datetime(2026, 8, 27, tzinfo=timezone.utc)
    scripted = (SearchRoundRunMode.SCRIPTED,)
    contents = (
        OperatorProfileContent(
            profile_id="m2-scripted-target",
            profile_version=1,
            profile_kind="target",
            state="active",
            allowed_run_modes=scripted,
            display_name="M2 Scripted Target",
            summary="Synthetic target authority for OX-1 control-flow validation only.",
            authority_refs=TargetOperatorProfileRefs(
                target_id="m2-scripted-target",
                target_spec_hash=_sha256("m2-scripted-target-spec-v1"),
                adapter_profile="m2-scripted-v1",
                resource_policy_id="m2-scripted-no-hcu",
                resource_policy_hash=_sha256("m2-scripted-no-hcu-v1"),
                candidate_package_store_id="m2-scripted-package-store",
                candidate_package_store_version=1,
                candidate_package_store_hash=_sha256("m2-scripted-package-store-v1"),
                required_stage0_protocol_hash=_sha256("m2-scripted-stage0-protocol-v1"),
            ),
            synthetic=True,
            created_at=created_at,
        ),
        OperatorProfileContent(
            profile_id="m2-scripted-workload",
            profile_version=1,
            profile_kind="workload",
            state="active",
            allowed_run_modes=scripted,
            display_name="M2 Scripted Workload",
            summary="Synthetic workload authority for deterministic SearchRound fixtures.",
            authority_refs=WorkloadOperatorProfileRefs(
                workload_id="m2-scripted-workload-v1",
                workload_hash=_sha256("m2-scripted-workload-v1"),
                configuration_hash=_sha256("m2-scripted-configuration-v1"),
                dataset_uri="fixture://m2-operator/dataset-v1",
                dataset_hash=_sha256("m2-scripted-dataset-v1"),
                model_uri="fixture://m2-operator/model-v1",
                model_hash=_sha256("m2-scripted-model-v1"),
                hotspot_scope_id="m2-scripted-fixture-hotspots",
                hotspot_scope_hash=_sha256("m2-scripted-fixture-hotspots-v1"),
                baseline_selection_policy="latest_frozen_matching",
            ),
            synthetic=True,
            created_at=created_at,
        ),
        OperatorProfileContent(
            profile_id="m2-scripted-standard",
            profile_version=1,
            profile_kind="measurement",
            state="active",
            allowed_run_modes=scripted,
            display_name="M2 Scripted Standard",
            summary="Synthetic budget and protocol references with no performance claim.",
            authority_refs=MeasurementOperatorProfileRefs(
                search_protocol_version="m2-scripted-search-v1",
                search_protocol_hash=_sha256("m2-scripted-search-v1"),
                holdout_protocol_version="m2-scripted-holdout-v1",
                holdout_protocol_hash=_sha256("m2-scripted-holdout-v1"),
                selection_rule_hash=_sha256("m2-scripted-selection-v1"),
                budget=RoundBudget(
                    max_candidates=4,
                    max_build_attempts=4,
                    max_correctness_attempts=4,
                    max_search_samples=16,
                    max_holdout_samples=8,
                    max_wall_seconds=600,
                    max_exclusive_lease_seconds=1,
                ),
                conclusion_boundary="standard",
            ),
            synthetic=True,
            created_at=created_at,
        ),
    )
    return OperatorProfileCatalog(tuple(publish_operator_profile(item) for item in contents))
