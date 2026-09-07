# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import importlib
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi.security import HTTPBasicCredentials
from fastapi.testclient import TestClient

from hcuopt.api.inspection_access import FileRunReadAccess, provision_run_read_access
from hcuopt.api.inspection_server import create_inspection_app

RUN = UUID(int=100)
PATH = f"/v1/operator/agent-generations/{RUN}/inspection"


@pytest.fixture
def client_case(tmp_path, monkeypatch):
    monkeypatch.setenv("HCUOPT_AUTO_MIGRATE", "true")
    repository = Mock()
    access = Mock()
    service = Mock(side_effect=AssertionError("no evidence service before authorization"))
    monkeypatch.setattr(
        importlib.import_module("hcuopt.api.inspection_server"),
        "AgentGenerationInspectionService",
        service,
    )
    app = create_inspection_app(repository=repository, evidence_root=tmp_path, access=access)
    return app, repository, access, service


def test_read_only_app_registers_only_two_get_routes_and_never_migrates(client_case):
    app, repository, access, service = client_case
    assert {(route.path, tuple(route.methods)) for route in app.routes} == {
        ("/healthz", ("GET",)),
        ("/v1/operator/agent-generations/{generation_run_id}/inspection", ("GET",)),
    }
    with TestClient(app, base_url="https://viewer.invalid") as client:
        assert client.get("/healthz").json()["scope"] == "read_only_liveness_not_readiness"
        for path in ("/v1/tasks", "/v1/operator/agent-generations", "/docs", "/openapi.json"):
            assert client.get(path).status_code == 404
            assert client.post(path, json={}).status_code == 404
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert client.request(method, PATH).status_code == 405
        response = client.get(PATH)
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Basic ")
        assert response.headers["cache-control"] == "no-store"
    assert not repository.mock_calls
    assert not access.mock_calls
    service.assert_not_called()


@pytest.mark.parametrize(
    "decision,code", [(False, 403), (1, 403), (RuntimeError("secret-error"), 503)]
)
def test_auth_failure_never_reads_evidence(client_case, decision, code):
    app, repository, access, service = client_case
    access.authorize.return_value = decision
    if isinstance(decision, Exception):
        access.authorize.side_effect = decision
    with TestClient(app, base_url="https://viewer.invalid") as client:
        response = client.get(PATH, auth=("operator", "test-not-a-real-password"))
    assert response.status_code == code
    assert response.headers["cache-control"] == "no-store"
    assert "secret-error" not in response.text
    assert access.authorize.call_args.args[1] == RUN
    assert not repository.mock_calls
    service.assert_not_called()


def test_plaintext_remote_and_spoofed_forwarded_headers_are_rejected(client_case):
    app, repository, access, service = client_case
    with TestClient(app, base_url="http://viewer.invalid") as client:
        response = client.get(PATH, headers={"x-forwarded-proto": "https"})
    assert response.status_code == 403
    assert "www-authenticate" not in response.headers
    assert not access.mock_calls
    service.assert_not_called()
    assert not repository.mock_calls


@pytest.mark.skipif(os.name != "posix", reason="native private permissions and secure Reader")
def test_private_access_is_scoped_expiring_and_revocable(tmp_path):
    tmp_path.chmod(0o700)
    config, secret = tmp_path / "access.json", tmp_path / "credential.txt"
    access = provision_run_read_access(config, secret, run_ids=(RUN,))
    password = secret.read_text().splitlines()[1].split(": ", 1)[1]
    assert password not in config.read_text()
    assert config.stat().st_mode & 0o077 == 0
    assert secret.stat().st_mode & 0o077 == 0
    credentials = HTTPBasicCredentials(username="operator", password=password)
    reader = FileRunReadAccess(config)
    assert reader.authorize(credentials, RUN)
    assert not reader.authorize(credentials, UUID(int=101))
    assert not reader.authorize(HTTPBasicCredentials(username="operator", password="wrong"), RUN)
    assert not FileRunReadAccess(config, clock=lambda: access.expires_at).authorize(
        credentials, RUN
    )
    changed = access.model_copy(update={"expires_at": datetime.now(timezone.utc) - timedelta(1)})
    config.write_text(changed.model_dump_json())
    assert not reader.authorize(credentials, RUN)  # no cached permission
    config.write_text("{}")
    with pytest.raises(ValueError):
        reader.authorize(credentials, RUN)


@pytest.mark.skipif(os.name != "posix", reason="native private permissions and secure Reader")
def test_access_rejects_shared_permissions_symlinks_and_overwrites(tmp_path):
    tmp_path.chmod(0o700)
    config, secret = tmp_path / "access.json", tmp_path / "credential.txt"
    provision_run_read_access(config, secret, run_ids=(RUN,))
    previous = secret.read_bytes()
    with pytest.raises(ValueError):
        provision_run_read_access(config, secret, run_ids=(RUN,))
    assert secret.read_bytes() == previous
    credentials = HTTPBasicCredentials(username="operator", password="irrelevant")
    config.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        FileRunReadAccess(config).authorize(credentials, RUN)
    config.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(config)
    with pytest.raises(ValueError, match="owner-only"):
        FileRunReadAccess(link).authorize(credentials, RUN)
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError, match="0700"):
        FileRunReadAccess(config).authorize(credentials, RUN)


@pytest.mark.parametrize("ttl", [0, True, 59, 86_401])
def test_provision_rejects_unbounded_lifetime_before_writing(tmp_path, ttl):
    with pytest.raises(ValueError, match="lifetime"):
        provision_run_read_access(tmp_path / "a", tmp_path / "b", run_ids=(RUN,), ttl_seconds=ttl)
    assert not list(tmp_path.iterdir())


def test_factory_requires_separate_credentials_and_sets_read_only_transactions(
    monkeypatch, tmp_path
):
    module = importlib.import_module("hcuopt.api.inspection_server")
    monkeypatch.setattr(
        module,
        "os",
        SimpleNamespace(
            **{
                "name": "posix",
                "getenv": Mock(return_value=None),
                "environ": {
                    "HCUOPT_INSPECTION_DATABASE_URL": "postgresql://reader@example.invalid/example",
                    "HCUOPT_AGENT_INSPECTION_ROOT": str(tmp_path),
                    "HCUOPT_INSPECTION_ACCESS_FILE": str(tmp_path / "access.json"),
                },
            }
        ),
    )
    repository = Mock()
    monkeypatch.setattr(module, "PostgresRepository", repository)
    factory = Mock()
    monkeypatch.setattr(module, "create_inspection_app", factory)
    module.app_from_environment()
    assert "default_transaction_read_only=on" in repository.call_args.args[0]
    repository.return_value.migrate.assert_not_called()
    module.os.getenv.return_value = "do-not-log"
    module.os.getenv.side_effect = None
    with pytest.raises(ValueError, match="model credential"):
        module.app_from_environment()
    assert factory.call_count == 1
