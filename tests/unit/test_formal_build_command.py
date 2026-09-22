# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json
from uuid import uuid4

import pytest

from hcuopt.deployment import formal_build_command as command


@pytest.mark.parametrize("fault", ["unknown", "boolean_budget", "escape", "permissions"])
def test_invocation_rejected_before_runtime_loading(tmp_path, monkeypatch, fault):
    if fault == "permissions" and command.os.name == "nt":
        pytest.skip("POSIX mode check; Windows ACLs are deployment-owned")
    token = uuid4()
    raw = dict(schema_version="formal-build-invocation-v1", intent_id=str(uuid4()),
               worker_id="worker", claim_token=str(token), wall_seconds_per_candidate=30,
               artifact_root="artifacts", cache_root="cache", output_root="output")
    for name in ("artifacts", "cache", "output"):
        (tmp_path / name).mkdir()
    if fault == "unknown":
        raw["ignore_authorization"] = True
    elif fault == "boolean_budget":
        raw["wall_seconds_per_candidate"] = True
    elif fault == "escape":
        raw["output_root"] = "../elsewhere"
    path = tmp_path / "invocation.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    path.chmod(0o644 if fault == "permissions" else 0o600)
    def unexpected(**kwargs):
        pytest.fail("must reject before runtime loading")
    monkeypatch.setattr(command, "load_formal_service_runtime", unexpected)
    with pytest.raises(ValueError):
        command.execute_build_command(
            deployment_root=tmp_path, configuration_path=tmp_path / "service.json",
            invocation_path=path, database_url="private-dsn", expected_source_commit="a" * 40,
        )


def test_cli_failure_redacts_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HCUOPT_DATABASE_URL", "private-dsn")
    monkeypatch.setattr("sys.argv", ["formal_build_command", "--deployment-root", str(tmp_path),
                                   "--configuration", "service.json", "--invocation", "claim.json",
                                   "--source-commit", "a" * 40])
    def fail(**kwargs):
        raise RuntimeError("private-dsn private-token")
    monkeypatch.setattr(command, "execute_build_command", fail)
    assert command.main() == 2
    output = capsys.readouterr().out
    assert "private" not in output
    assert json.loads(output)["automatic_retry_allowed"] is False
