# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from hcuopt.deployment import kernel_binary_inventory as inventory


class Distribution:
    version = "fixture-only"

    def __init__(self, root, files):
        self.root, self.files = root, files

    def locate_file(self, path):
        return self.root / path


def test_inventory_hashes_files_without_importing_kernel_code(tmp_path, monkeypatch):
    package = tmp_path / "sgl_kernel"
    package.mkdir()
    (package / "__init__.py").write_text("raise RuntimeError('must not import')")
    library = package / "kernel.so"
    library.write_bytes(b"fixture-not-a-real-library")
    dist_info = tmp_path / "sgl_kernel.dist-info"
    dist_info.mkdir()
    (dist_info / "RECORD").write_text("fixture manifest")
    dist = Distribution(tmp_path, ["sgl_kernel/__init__.py", "sgl_kernel/kernel.so",
                                  "sgl_kernel.dist-info/RECORD"])
    monkeypatch.setattr(inventory.metadata, "distribution", lambda _: dist)
    report = inventory.collect()
    item = report["packages"][0]
    assert item["status"] == "inventory_collected"
    assert item["version"] == "fixture-only"
    assert item["libraries"][0]["sha256"] == "sha256:" + hashlib.sha256(
        library.read_bytes()).hexdigest()
    assert item["record_sha256"].startswith("sha256:")
    assert report["device_execution_performed"] is False
    assert report["loaded_dispatch_identity"] == "not_verified"
    assert report["automatic_release_allowed"] is False


def test_missing_distribution_and_manifest_are_not_success(tmp_path, monkeypatch):
    def missing(_):
        raise inventory.metadata.PackageNotFoundError
    monkeypatch.setattr(inventory.metadata, "distribution", missing)
    assert inventory.inspect_distribution("fixture")["status"] == "not_installed"
    monkeypatch.setattr(inventory.metadata, "distribution", lambda _: Distribution(tmp_path, None))
    result = inventory.inspect_distribution("fixture")
    assert result["status"] == "incomplete"
    assert result["errors"] == ["distribution_file_manifest_unavailable"]


def test_escaping_or_missing_library_manifest_is_reported(tmp_path, monkeypatch):
    root = tmp_path / "site-packages"
    root.mkdir()
    (tmp_path / "outside.so").write_bytes(b"do not hash")
    dist = Distribution(root, [Path("../outside.so"), Path("missing.so")])
    monkeypatch.setattr(inventory.metadata, "distribution", lambda _: dist)
    result = inventory.inspect_distribution("fixture")
    assert result["status"] == "incomplete"
    assert len(result["errors"]) == 2
    assert result["libraries"] == []


def test_no_site_discovery_reads_metadata_without_executing_startup(tmp_path):
    dist_info = tmp_path / "sgl_kernel-1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Name: sgl-kernel\nVersion: 1.0\n")
    (tmp_path / "unsafe.pth").write_text("import this_module_must_not_be_executed\n")
    found = inventory.discover_distribution("sgl_kernel", [str(tmp_path)])
    assert found.version == "1.0"


def test_source_declared_das_distribution_is_queried_separately():
    assert "sglang-kernel" in inventory.PACKAGES
    assert "sgl-kernel" in inventory.PACKAGES


def test_standalone_no_site_entrypoint_does_not_import_kernel_packages():
    completed = subprocess.run(
        [sys.executable, "-I", "-S", str(Path(inventory.__file__).resolve())],
        capture_output=True, text=True, check=True,
    )
    report = json.loads(completed.stdout)
    assert report["site_startup_disabled"] is True
    assert report["kernel_modules_imported"] is False
    assert report["device_execution_performed"] is False
