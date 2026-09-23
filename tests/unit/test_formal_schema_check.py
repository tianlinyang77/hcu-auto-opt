# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json

from hcuopt.deployment import formal_schema_check as module


def test_missing_configuration_does_not_connect(monkeypatch, capsys):
    monkeypatch.delenv("HCUOPT_DATABASE_URL", raising=False)
    monkeypatch.setattr(module, "inspect_formal_schema", lambda _: 1 / 0)
    assert module.main() == 2
    assert "required" in capsys.readouterr().out


def test_driver_errors_do_not_expose_credentials(monkeypatch, capsys):
    monkeypatch.setenv("HCUOPT_DATABASE_URL", "secret-dsn")

    def fail(_):
        raise RuntimeError("secret-dsn password=private")

    monkeypatch.setattr(module, "inspect_formal_schema", fail)
    assert module.main() == 2
    output = capsys.readouterr().out
    assert "secret-dsn" not in output and "private" not in output


def test_incomplete_schema_exit_code(monkeypatch, capsys):
    monkeypatch.setenv("HCUOPT_DATABASE_URL", "private")
    monkeypatch.setattr(module, "inspect_formal_schema", lambda _: {
        "schema_checks_passed": False, "execution_authorized": False,
    })
    assert module.main() == 1
    assert json.loads(capsys.readouterr().out)["execution_authorized"] is False
