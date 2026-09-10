# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import ast
import json
from pathlib import Path, PurePosixPath

import pytest

from hcuopt.deployment import bw20_stage0_preflight as probe


def test_standalone_script_keeps_old_host_python_syntax():
    source = Path(probe.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 6))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "__future__"
        # builtin generics parse on 3.6 but fail when annotations are evaluated.
        annotation = getattr(node, "annotation", None)
        if isinstance(node, ast.FunctionDef):
            annotation = node.returns
        if annotation is not None:
            assert isinstance(annotation, ast.Name)


def test_missing_observations_never_become_idle_or_authorized(tmp_path):
    result = probe.collect(tmp_path)
    assert result["device"]["status"] == "unavailable"
    assert result["kfd"]["processes"] is None
    assert result["measurement_window"] == "not_verified"
    for field in ("clock_policy_authorized", "target_snapshot_bound", "stage0_accepted",
                  "automatic_release_allowed"):
        assert result[field] is False
    assert result["performance_conclusion"] == "not_measured"


def test_raw_frequency_and_memory_are_preserved_without_interpreting(tmp_path, monkeypatch):
    device = tmp_path / "device"
    device.mkdir()
    for name in probe.DEVICE_FILES:
        (device / name).write_text("0\n", encoding="utf-8")
    (device / "pp_dpm_sclk").write_text("0: 600Mhz *\n1: 1500Mhz\n", encoding="utf-8")
    result = probe._device(device)
    assert result["expected_pci_matches"] is False
    assert result["files"]["pp_dpm_sclk"]["raw"] == "0: 600Mhz *\n1: 1500Mhz\n"
    assert "measurement_window" not in result
    assert probe._device(tmp_path)["expected_pci_matches"] is False
    # Linux PCI names contain colons, which cannot be created on Windows.
    monkeypatch.setattr(probe, "_read", lambda path: {"status": "missing"})
    assert probe._device(PurePosixPath("/sys") / probe.EXPECTED_PCI)["expected_pci_matches"]


@pytest.mark.parametrize("error,status", [
    (PermissionError, "permission_denied"), (OSError, "unreadable"),
    (FileNotFoundError, "missing"),
])
def test_read_failure_explicit_not_zero(tmp_path, monkeypatch, error, status):
    def fail(*args, **kwargs):
        raise error()
    monkeypatch.setattr(Path, "open", fail)
    assert probe._read(tmp_path) == {"status": status}


def test_read_size_is_bounded(tmp_path):
    path = tmp_path / "raw"
    path.write_text("a" * 8193, encoding="utf-8")
    assert probe._read(path) == {"status": "oversized"}


def test_global_kfd_pid_list_does_not_assert_per_device_ownership(tmp_path):
    (tmp_path / "23").mkdir()
    (tmp_path / "9").mkdir()
    (tmp_path / "not-a-pid").mkdir()
    result = probe._kfd_processes(tmp_path)
    assert result["processes"] == [9, 23]
    assert result["device_attribution"] == "not_established"


def test_permission_denied_kfd_is_not_empty(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError()
    monkeypatch.setattr(Path, "iterdir", denied)
    assert probe._kfd_processes(tmp_path) == {"status": "permission_denied", "processes": None}


def test_cli_reports_incomplete_without_mutations(tmp_path, monkeypatch, capsys):
    result = probe.collect(tmp_path)
    monkeypatch.setattr(probe, "collect", lambda: result)
    assert probe.main() == 2
    assert json.loads(capsys.readouterr().out)["stage0_accepted"] is False


def test_complete_observation_exit_zero_still_not_authority(tmp_path, monkeypatch, capsys):
    result = probe.collect(tmp_path)
    result.update(expected_host_matches=True, device={
        "expected_pci_matches": True,
        "files": {name: {"status": "observed", "raw": "0"} for name in probe.DEVICE_FILES},
    }, kfd={"status": "observed", "processes": []})
    monkeypatch.setattr(probe, "collect", lambda: result)
    assert probe.main() == 0
    assert json.loads(capsys.readouterr().out)["measurement_window"] == "not_verified"
