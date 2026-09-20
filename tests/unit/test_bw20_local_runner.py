# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import sys
from uuid import uuid4

import pytest

from hcuopt.deployment import bw20_local_runner as module


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "node", lambda: "github-bw20")
    monkeypatch.setattr(module.getpass, "getuser", lambda: "github")
    monkeypatch.setattr(module.os, "geteuid", lambda: 1002, raising=False)


def test_local_runner_keeps_byte_contract_without_ssh(host):
    runner = module.BW20LocalCommandRunner()
    result = runner.run((sys.executable, "-c", "print('local-cpu-only')"))
    assert result.returncode == 0 and result.stdout.strip() == b"local-cpu-only"
    assert runner.wrapped_argv(("docker", "version")) == ("docker", "version")


@pytest.mark.parametrize("field", ["system", "node", "user", "root"])
def test_wrong_host_or_identity_fails(host, monkeypatch, field):
    owner, name, value = {
        "system": (module.platform, "system", "Windows"),
        "node": (module.platform, "node", "nmz36"),
        "user": (module.getpass, "getuser", "another-user"),
        "root": (module.os, "geteuid", 0),
    }[field]
    monkeypatch.setattr(owner, name, lambda: value)
    with pytest.raises(ValueError, match="non-root BW20"):
        module.BW20LocalCommandRunner()


@pytest.mark.parametrize("argv", [(), ("",), ("docker", None), ("bad\0entry",)])
def test_bad_command_refused(host, argv):
    with pytest.raises(ValueError):
        module.BW20LocalCommandRunner().wrapped_argv(argv)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX archive no-follow contract")
def test_local_archive_copy_is_exclusive_and_scoped(host, monkeypatch, tmp_path):
    monkeypatch.setattr(module, "ROOT", str(tmp_path))
    source = tmp_path / "source.tar"
    source.write_bytes(b"immutable archive")
    run = tmp_path / str(uuid4())
    run.mkdir()
    destination = run / "controller.tar"
    runner = module.BW20LocalCommandRunner()
    runner.copy_controller_archive(source, destination)
    assert destination.read_bytes() == source.read_bytes()
    with pytest.raises(FileExistsError):
        runner.copy_controller_archive(source, destination)
    with pytest.raises(ValueError):
        runner.copy_controller_archive(source, run / "other.tar")
