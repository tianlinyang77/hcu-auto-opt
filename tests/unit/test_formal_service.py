# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Startup tests use signed fixture authority and actual candidate package bytes."""

import json

import pytest
from fastapi.testclient import TestClient

from hcuopt.deployment import formal_service
from hcuopt.domain.errors import SourceArtifactError
from tests.unit.test_formal_compiler import configuration
from tests.unit.test_formal_operator_plans import MOUNT_TARGET, REPLACEMENT_POINT


def service_fixture(tmp_path):
    compiler, kwargs = configuration(tmp_path)
    for name in ("previews", "authorities", "web", "web/assets"):
        (tmp_path / name).mkdir()
    (tmp_path / "web/index.html").write_text("<html>Formal console</html>", encoding="utf-8")
    (tmp_path / "capabilities.json").write_text(json.dumps({
        "schema_version": "formal-intent-capabilities-v1", "capabilities": [],
    }), encoding="utf-8")
    raw = dict(
        schema_version="formal-service-configuration-v1", compiler_path="compiler.json",
        trust_path="trust.json", capabilities_path="capabilities.json", package_root="packages",
        preview_root="previews", authority_root="authorities", static_root="web",
        package_profile="test-formal-service",
        store_id=compiler.candidate_family.source_package_store_id,
        store_hash=compiler.candidate_family.source_package_store_hash,
        allowed_overlay_roots=["sglang"], approved_mount_targets={REPLACEMENT_POINT: MOUNT_TARGET},
        port=4205,
    )
    path = tmp_path / "service.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return raw, dict(deployment_root=tmp_path, configuration_path=path,
                     database_url="unused-test-dsn", expected_source_commit="a" * 40,
                     clock=kwargs["clock"])


def test_service_loads_packages_and_serves_without_dispatch(tmp_path, monkeypatch):
    _, kwargs = service_fixture(tmp_path)
    calls = []
    monkeypatch.setattr(formal_service, "inspect_formal_schema", lambda dsn: (
        calls.append(dsn) or {"schema_checks_passed": True}
    ))
    app, port = formal_service.build_formal_service(**kwargs)
    assert calls == ["unused-test-dsn"]
    assert port == 4205
    assert not app.state.formal_runtime.dispatcher.enabled
    with TestClient(app, base_url="http://127.0.0.1:4205") as client:
        assert client.get("/").status_code == 200
        assert client.get("/", headers={"host": "evil.example"}).status_code == 403


@pytest.mark.parametrize("fault", ["package", "store", "path", "schema", "unknown"])
def test_service_rejects_invalid_inputs(tmp_path, monkeypatch, fault):
    raw, kwargs = service_fixture(tmp_path)
    monkeypatch.setattr(formal_service, "inspect_formal_schema",
                        lambda _: {"schema_checks_passed": fault != "schema"})
    if fault == "package":
        source = next((tmp_path / "packages").rglob("allocator.py"))
        source.write_text("tampered", encoding="utf-8")
    elif fault == "store":
        raw["store_id"] = "different-store"
    elif fault == "path":
        raw["package_root"] = "../escape"
    elif fault == "unknown":
        raw["database_password"] = "must-not-be-configured-here"
    kwargs["configuration_path"].write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises((ValueError, SourceArtifactError)):
        formal_service.build_formal_service(**kwargs)


def test_cli_redacts_startup_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HCUOPT_DATABASE_URL", "secret-dsn")
    monkeypatch.setattr("sys.argv", ["formal_service", "--deployment-root", str(tmp_path),
                                   "--configuration", "missing.json", "--source-commit", "a" * 40])
    assert formal_service.main() == 2
    output = capsys.readouterr().out
    assert json.loads(output)["started"] is False
    assert "secret-dsn" not in output
