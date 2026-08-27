# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hcuopt.api.app import create_app
from hcuopt.contracts.operator_v1 import (
    MeasurementOperatorProfileRefs,
    OperatorProfileContent,
    OperatorProfileDescriptor,
    OperatorProfileRef,
    TargetOperatorProfileRefs,
)
from hcuopt.domain.enums import SearchRoundRunMode
from hcuopt.operator import (
    OperatorProfileCatalog,
    build_operator_service_identity,
    build_scripted_operator_profile_catalog,
    publish_operator_profile,
)
from hcuopt.operator.errors import (
    OperatorPlanHashMismatch,
    OperatorProfileModeMismatch,
    OperatorProfileNotFound,
    OperatorProfileRevoked,
)


def test_scripted_catalog_is_deterministic_and_explicitly_synthetic() -> None:
    catalog = build_scripted_operator_profile_catalog()
    profiles = catalog.list()

    assert [profile.profile_kind for profile in profiles] == [
        "measurement",
        "target",
        "workload",
    ]
    assert all(profile.synthetic for profile in profiles)
    assert all(
        profile.allowed_run_modes == (SearchRoundRunMode.SCRIPTED,)
        for profile in profiles
    )
    assert OperatorProfileCatalog(tuple(reversed(profiles))).catalog_hash == (
        catalog.catalog_hash
    )


def test_catalog_rejects_profile_hash_drift_and_duplicate_identity() -> None:
    profile = build_scripted_operator_profile_catalog().list("target")[0]

    with pytest.raises(ValidationError, match="frozen_instance"):
        profile.summary = "changed"
    with pytest.raises(ValueError, match="profile_hash"):
        OperatorProfileCatalog((profile.model_copy(update={"summary": "changed"}),))
    with pytest.raises(ValueError, match="identity"):
        OperatorProfileCatalog((profile, profile))


def test_catalog_requires_exact_hash_state_and_run_mode() -> None:
    catalog = build_scripted_operator_profile_catalog()
    profile = catalog.list("measurement")[0]
    reference = OperatorProfileRef(
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        profile_kind=profile.profile_kind,
        profile_hash=profile.profile_hash,
    )

    assert catalog.require(reference, SearchRoundRunMode.SCRIPTED) == profile
    with pytest.raises(OperatorPlanHashMismatch, match="Profile Hash"):
        catalog.require(
            reference.model_copy(update={"profile_hash": "sha256:" + "0" * 64}),
            SearchRoundRunMode.SCRIPTED,
        )
    with pytest.raises(OperatorProfileModeMismatch, match="run mode"):
        catalog.require(reference, SearchRoundRunMode.FORMAL)
    with pytest.raises(OperatorProfileNotFound, match="not registered"):
        catalog.require(
            reference.model_copy(update={"profile_version": 2}),
            SearchRoundRunMode.SCRIPTED,
        )

    revoked_content = OperatorProfileContent.model_validate(
        {
            **profile.model_dump(mode="json", exclude={"profile_hash"}),
            "state": "revoked",
        }
    )
    revoked = publish_operator_profile(revoked_content)
    revoked_catalog = OperatorProfileCatalog((revoked,))
    revoked_reference = reference.model_copy(update={"profile_hash": revoked.profile_hash})
    with pytest.raises(OperatorProfileRevoked, match="cannot start"):
        revoked_catalog.require(revoked_reference, SearchRoundRunMode.SCRIPTED)


def test_catalog_rejects_real_profile_without_separate_authorization() -> None:
    scripted = build_scripted_operator_profile_catalog().list("target")[0]
    content = OperatorProfileContent.model_validate(
        {
            **scripted.model_dump(mode="json", exclude={"profile_hash"}),
            "profile_id": "formal-target-disabled",
            "synthetic": False,
            "allowed_run_modes": ["formal"],
        }
    )

    with pytest.raises(ValueError, match="separate authorization"):
        OperatorProfileCatalog((publish_operator_profile(content),))


def test_profile_kind_and_authority_refs_are_strictly_bound() -> None:
    profile = build_scripted_operator_profile_catalog().list("target")[0]
    target = TargetOperatorProfileRefs.model_validate(profile.authority_refs)
    measurement = MeasurementOperatorProfileRefs(
        search_protocol_version="search-v1",
        search_protocol_hash="sha256:" + "1" * 64,
        holdout_protocol_version="holdout-v1",
        holdout_protocol_hash="sha256:" + "2" * 64,
        selection_rule_hash="sha256:" + "3" * 64,
        budget=build_scripted_operator_profile_catalog()
        .list("measurement")[0]
        .authority_refs.budget,
        conclusion_boundary="standard",
    )
    assert target.target_id == "m2-scripted-target"

    with pytest.raises(ValidationError, match="profile_kind"):
        OperatorProfileDescriptor.model_validate(
            {**profile.model_dump(mode="json"), "authority_refs": measurement}
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        OperatorProfileContent.model_validate(
            {
                **profile.model_dump(mode="json", exclude={"profile_hash"}),
                "unapproved_default": True,
            }
        )


def test_service_identity_binds_catalog_and_deployment_generation() -> None:
    catalog = build_scripted_operator_profile_catalog()
    generation = uuid4()
    identity = build_operator_service_identity(
        source_commit="a" * 40,
        server_instance_id=generation,
        catalog=catalog,
    )

    assert identity.operator_contract_version == "m2-operator-v1"
    assert identity.control_contract_version == "m2a-search-round-v1"
    assert identity.profile_catalog_hash == catalog.catalog_hash
    assert identity.server_instance_id == generation


def test_operator_profile_api_lists_and_shows_only_scripted_profiles() -> None:
    repository = MagicMock()
    with TestClient(create_app(repository=repository)) as client:
        listed = client.get("/v1/operator/profiles", params={"profile_kind": "target"})
        shown = client.get("/v1/operator/profiles/target/m2-scripted-target/versions/1")
        missing = client.get("/v1/operator/profiles/target/missing-profile/versions/1")

    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["synthetic"] is True
    assert listed.json()[0]["allowed_run_modes"] == ["scripted"]
    assert shown.status_code == 200
    assert shown.json()["profile_hash"] == listed.json()[0]["profile_hash"]
    assert missing.status_code == 404
    assert missing.json()["code"] == "operator_profile_not_found"
    assert missing.json()["retryable"] is False
