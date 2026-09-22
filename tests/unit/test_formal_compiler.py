# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Configuration admission tests with explicit synthetic readiness evidence."""

import json
from uuid import uuid4

import pytest

from hcuopt.adapters.business_candidate_family import business_candidate_source_family_hash
from hcuopt.contracts.formal_profile_authorization_v1 import (
    FormalProfileWindowAuthorizationContent,
    formal_profile_window_authorization_hash,
    publish_formal_profile_window_authorization,
)
from hcuopt.contracts.formal_readiness_v1 import FormalReadinessManifest
from hcuopt.deployment.formal_compiler import FormalCompilerConfiguration, load_formal_compiler
from hcuopt.deployment.formal_runtime import FormalDeploymentRuntime
from hcuopt.deployment.formal_signing import FormalOwnerEd25519Signer
from hcuopt.deployment.formal_trust import FormalPublicTrust
from hcuopt.operator.errors import OperatorFormalAuthorizationInvalid
from hcuopt.operator.readiness import formal_readiness_manifest_hash, formal_readiness_report_hash
from tests.unit import test_formal_operator_profile_authorization as admission
from tests.unit.test_formal_operator_plans import _fixture
from tests.unit.test_formal_trust import trust_bundle


def configuration(tmp_path):
    fixture = _fixture(tmp_path / "packages")
    profiles, manifest, _, old, _ = admission._authorization_fixture()
    family_hash = business_candidate_source_family_hash(fixture.family)
    raw = manifest.model_dump(mode="json")
    raw["profile_draft"]["candidate_family_hash"] = family_hash
    manifest = FormalReadinessManifest.model_validate(raw)
    report = admission._ready_report(manifest)
    keys, bundle = trust_bundle()
    trust_path = tmp_path / "trust.json"
    trust_path.write_text(json.dumps(bundle), encoding="utf-8")
    trust = FormalPublicTrust.from_file(deployment_root=tmp_path, path=trust_path)
    signer = FormalOwnerEd25519Signer(keys["owner"], verifier_id="test.owner", key_id="owner-v1")
    content = FormalProfileWindowAuthorizationContent.model_validate({
        **old.model_dump(mode="json", exclude={"authorization_hash", "signature"}),
        "source_family_hash": family_hash, "verifier": signer.verifier_ref,
        "readiness_manifest_hash": formal_readiness_manifest_hash(manifest),
        "readiness_report_hash": formal_readiness_report_hash(report),
    })
    authorization = publish_formal_profile_window_authorization(content,
        signature=signer.sign_authorization(
            authorization_hash=formal_profile_window_authorization_hash(content),
        ))
    config = FormalCompilerConfiguration(
        schema_version="formal-compiler-configuration-v1", source_commit="a" * 40,
        server_instance_id=uuid4(), profiles=profiles, authorization=authorization,
        readiness_manifest=manifest, readiness_report=report, candidate_family=fixture.family,
    )
    path = tmp_path / "compiler.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    kwargs = dict(deployment_root=tmp_path, path=path, trust=trust,
                  candidate_family_verifier=fixture.compiler.candidate_family_verifier,
                  expected_source_commit="a" * 40, clock=lambda: admission.FIXED_TIME)
    return config, kwargs


def test_configuration_rebuilds_admitted_compiler_and_runtime(tmp_path):
    config, kwargs = configuration(tmp_path)
    first, second = load_formal_compiler(**kwargs), load_formal_compiler(**kwargs)
    assert first.service_identity == second.service_identity
    assert first.profiles.formal_authorization_hash == config.authorization.authorization_hash
    family = first.candidate_family_manifest_store.read_manifest(
        source_family_hash=config.authorization.source_family_hash,
    )
    assert family == config.candidate_family
    access = tmp_path / "capabilities.json"
    access.write_text(json.dumps({"schema_version": "formal-intent-capabilities-v1",
                                 "capabilities": []}), encoding="utf-8")
    runtime = FormalDeploymentRuntime.from_configuration(
        object(), deployment_root=tmp_path, compiler_path=kwargs["path"],
        trust_path=tmp_path / "trust.json", capabilities_path=access,
        candidate_family_verifier=kwargs["candidate_family_verifier"],
        expected_source_commit="a" * 40, object_store=object(), clock=kwargs["clock"],
    )
    assert not runtime.dispatcher.enabled
    assert runtime.management.coordinator.compiler.service_identity == first.service_identity


@pytest.mark.parametrize("fault", ["release", "expired", "signature", "readiness", "family"])
def test_configuration_does_not_bypass_existing_admission(tmp_path, fault):
    config, kwargs = configuration(tmp_path)
    raw = config.model_dump(mode="json")
    if fault == "release":
        kwargs["expected_source_commit"] = "b" * 40
    elif fault == "expired":
        kwargs["clock"] = lambda: admission.WINDOW_EXPIRES_AT
    elif fault == "signature":
        raw["authorization"]["signature"] = "A" * 88
    elif fault == "readiness":
        raw["readiness_report"]["verified_evidence_count"] += 1
    else:
        raw["candidate_family"]["reviewed_by"] = "different-reviewer"
    kwargs["path"].write_text(json.dumps(raw), encoding="utf-8")
    expected_error = (
        ValueError if fault in {"release", "family"} else OperatorFormalAuthorizationInvalid
    )
    with pytest.raises(expected_error):
        load_formal_compiler(**kwargs)


@pytest.mark.parametrize("fault", ["unknown", "private", "oversize", "escape", "missing"])
def test_configuration_rejects_invalid_deployment_files(tmp_path, fault):
    config, kwargs = configuration(tmp_path)
    raw = config.model_dump(mode="json")
    if fault in {"unknown", "private"}:
        raw["unknown" if fault == "unknown" else "private_key"] = "not-allowed"
        kwargs["path"].write_text(json.dumps(raw), encoding="utf-8")
    elif fault == "oversize":
        kwargs["path"].write_bytes(b" " * (2 * 1024 * 1024 + 1))
    elif fault == "escape":
        kwargs["deployment_root"] = tmp_path / "separate-root"
        kwargs["deployment_root"].mkdir()
    else:
        kwargs["path"] = tmp_path / "absent.json"
    with pytest.raises(ValueError, match="invalid or unavailable"):
        load_formal_compiler(**kwargs)
