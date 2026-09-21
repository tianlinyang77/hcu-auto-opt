# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hcuopt.deployment.formal_runtime import FormalDeploymentRuntime
from hcuopt.domain.errors import Conflict
from tests.unit.formal_signed_fixture import setup_signed_management as setup_management
from tests.unit.test_formal_start_management_api import TOKEN


def test_runtime_composes_console_without_implicit_dispatch(tmp_path):
    management, repo, payload = setup_management(tmp_path)
    runtime = FormalDeploymentRuntime(repo, management)
    assert runtime.dispatcher.repository is repo
    assert runtime.claims.dispatcher is runtime.dispatcher
    assert not runtime.dispatcher.enabled and not runtime.claims.enabled
    root = tmp_path / "web"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("fixture", encoding="utf-8")
    app = runtime.console(static_root=root, browser_origin="http://127.0.0.1:4198")
    with TestClient(app, base_url="http://127.0.0.1:4198") as client:
        result = client.post(
            "/v1/operator/formal-start-intents", json=payload,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert result.status_code == 200
        assert result.json()["hcu_accessed"] is False
        assert client.get("/v1/operator/formal-round-dispatch").status_code == 403
        assert client.post("/v1/operator/formal-round-dispatch").status_code == 405
        assert client.get("/v1/operator/formal-correctness-recovery").status_code == 404
        assert client.get("/v1/jobs").status_code == 404
    assert len(repo.intents) == 1


@pytest.mark.parametrize("field", ["dispatch_reader", "recovery_reader"])
def test_runtime_rejects_prebound_readers(tmp_path, field):
    management, repo, _ = setup_management(tmp_path)
    with pytest.raises(Conflict, match="unbound"):
        FormalDeploymentRuntime(repo, replace(management, **{field: object()}))


def test_runtime_rejects_foreign_recovery_journal_before_serving(tmp_path):
    management, repo, _ = setup_management(tmp_path)
    runtime = FormalDeploymentRuntime(repo, management)
    foreign = SimpleNamespace(lease=SimpleNamespace(jobs=SimpleNamespace(claims=object())))
    with pytest.raises(Conflict, match="another runtime"):
        runtime.console(static_root=tmp_path, browser_origin="http://127.0.0.1:4198",
                        correctness_journal=foreign)


def test_runtime_rejects_truthy_string_enable(tmp_path):
    management, repo, _ = setup_management(tmp_path)
    with pytest.raises(ValueError, match="boolean"):
        FormalDeploymentRuntime(repo, management, enabled="false")


@pytest.mark.parametrize("role", ["actor", "execution", "evaluation"])
def test_signed_runtime_rejects_tampered_signature(tmp_path, role):
    management, repo, payload = setup_management(tmp_path)
    if role == "actor":
        capability = management.capabilities[0]
        management = replace(management, capabilities=(replace(
            capability, assertion=capability.assertion.model_copy(update={"signature": "A" * 88}),
        ),))
    else:
        store = management.coordinator.object_store
        authority = getattr(store, role)
        setattr(store, role, authority.model_copy(update={"signature": "A" * 88}))
    runtime = FormalDeploymentRuntime(repo, management)
    root = tmp_path / "signed-web"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("fixture", encoding="utf-8")
    with TestClient(runtime.console(static_root=root, browser_origin="http://127.0.0.1:4198"),
                    base_url="http://127.0.0.1:4198") as client:
        response = client.post("/v1/operator/formal-start-intents", json=payload,
                               headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code != 200 or response.json()["state"] != "ready_for_round_creation"
    assert all(intent.state != "ready_for_round_creation" for intent in repo.intents.values())
    if role == "actor":
        assert not repo.intents
