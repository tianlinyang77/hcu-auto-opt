# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from pathlib import Path
from uuid import uuid4

import pytest

from hcuopt.adapters.bw20_execution import RUN_PARENT, BW20SmokeExecutionAdapter
from hcuopt.adapters.execution import OpenSSHCommandRunner
from hcuopt.adapters.profiles import AdapterProfileCatalog
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.deployment.bw20_build_transport import BW20BuildJobHandler
from hcuopt.deployment.bw20_build_worker import PROFILE
from hcuopt.deployment.bw20_pair_bridge import (
    BW20PairCleaner,
    BW20Transport,
    PreparedBW20Evaluator,
)
from hcuopt.deployment.bw20_profile import compose_framework_profile
from hcuopt.domain.errors import AdapterUnavailable, TargetNotReady
from tests.unit.test_bw20_execution import TARGET


def parts(tmp_path, profile=PROFILE):
    # Construct boundaries without any remote calls; not deployment/HCU evidence.
    transport = BW20Transport(OpenSSHCommandRunner("10.17.1.20", user="github"))
    build = BW20BuildJobHandler(runner=transport.runner,
        controller_root="/home/github/hcu-auto-opt-runtime/bw20-api-controller-" + str(uuid4()),
        controller_manifest_sha256="sha256:" + "1" * 64, output_dir=tmp_path)
    registry = AdapterRegistry(profile=profile,
        executor=BW20SmokeExecutionAdapter(transport.runner,
            run_root=f"{RUN_PARENT}/{uuid4()}", profile=profile),
        evaluator=PreparedBW20Evaluator(transport=transport, snapshot=None,
            artifact_transport_path=Path("fixture-only"), run_id=uuid4(), profile=profile),
        resource_cleaner=BW20PairCleaner(TARGET, transport, (uuid4(), uuid4()), profile=profile))
    return build, registry


def test_opt_in_profile_does_not_silently_clear_target(tmp_path):
    build, registry = parts(tmp_path)
    profile = compose_framework_profile(build_handler=build, gpu_registry=registry)
    profile.require_framework_smoke()
    with pytest.raises(AdapterUnavailable, match="not registered"):
        AdapterProfileCatalog().require(PROFILE)
    with pytest.raises(TargetNotReady, match="open blockers"):
        profile.validate_target(TARGET)
    with pytest.raises(TargetNotReady, match="cannot grant"):
        profile.validate_target(TARGET, scope="stage0")
    with pytest.raises(TargetNotReady, match="cannot grant"):
        profile.validate_target(TARGET, evidence_resolved_blockers=frozenset({"any"}))


def test_unregistered_or_mixed_profile_rejected(tmp_path):
    build, registry = parts(tmp_path, "bw20-smoke-policy-unregistered-v1")
    with pytest.raises(AdapterUnavailable, match="same explicit"):
        compose_framework_profile(build_handler=build, gpu_registry=registry)


def test_fake_registry_cannot_register_real_profile(tmp_path):
    build, _ = parts(tmp_path)
    with pytest.raises(AdapterUnavailable):
        compose_framework_profile(build_handler=build, gpu_registry=AdapterRegistry.fake())


def test_real_api_refuses_open_target_without_repository_mutation(tmp_path):
    from fastapi.testclient import TestClient

    from hcuopt.api.app import create_app

    class NoWriteRepository:
        def migrate(self):
            pass

        def create_framework_smoke_task(self, *args):
            raise AssertionError("blocked target must never be written")

    build, registry = parts(tmp_path)
    profile = compose_framework_profile(build_handler=build, gpu_registry=registry)
    app = create_app(repository=NoWriteRepository(),
                     adapter_profiles=AdapterProfileCatalog((profile,)))
    with TestClient(app) as client:
        response = client.post("/v1/framework-smoke/tasks", json={
            "name": "BW20 blocked admission fixture", "target_id": TARGET.target_id,
            "adapter_profile": PROFILE, "idempotency_key": str(uuid4())})
    assert response.status_code == 409
    assert response.json()["code"] == "target_not_ready"
    assert "source_runtime_equivalence_unverified" in response.json()["message"]
