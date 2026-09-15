# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import ast
import json
import tarfile
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hcuopt.deployment.bw20_stage0_staging import (
    INIT_UPLOAD,
    UNPACK_UPLOAD,
    freeze_controller,
    stage_controller,
)
from tests.unit.test_bw20_stage0_runtime import plan


def source(tmp_path):
    root = tmp_path / "repository"
    dest = root / "src/hcuopt/deployment"
    dest.mkdir(parents=True)
    (dest / "bw20_stage0_worker.py").write_text("# trusted source\n")
    assets = dest / "assets"
    assets.mkdir()
    (assets / "bw20-passwd").write_bytes(
        b"root:x:0:0:root:/root:/bin/sh\n"
        b"github:x:1002:1002:BW20 Stage0:/tmp:/usr/sbin/nologin\n"
        b"nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"
    )
    (assets / "bw20-group").write_bytes(
        b"root:x:0:\ngithub:x:1002:\nnogroup:x:65534:\n"
    )
    (root / "secret.env").write_text("must not be uploaded")
    return root


def test_freeze_is_deterministic_and_excludes_non_source(tmp_path):
    root = source(tmp_path)
    first = freeze_controller(root, tmp_path / "first.tar")
    second = freeze_controller(root, tmp_path / "second.tar")
    assert first.archive_sha256 == second.archive_sha256
    assert first.manifest_sha256 == second.manifest_sha256
    with tarfile.open(first.archive) as tar:
        assert set(tar.getnames()) == {
            "controller-manifest.json", "src/hcuopt/deployment/bw20_stage0_worker.py",
            "src/hcuopt/deployment/assets/bw20-passwd",
            "src/hcuopt/deployment/assets/bw20-group"}
        manifest = json.load(tar.extractfile("controller-manifest.json"))
        assert len(manifest) == 3
        assert all(member.isfile() and member.mode == 0o444 for member in tar)


def test_freeze_includes_packaged_protocols_but_not_unrelated_yaml(tmp_path):
    root = source(tmp_path)
    protocols = root / "src/hcuopt/evaluation/protocols"
    protocols.mkdir(parents=True)
    (protocols / "s0-g0-bw20-v1.yaml").write_text("protocol_version: s0-g0-bw20-v1\n")
    (root / "src/credentials.yaml").write_text("not controller protocol data\n")
    sql = root / "src/hcuopt/storage/sql"
    sql.mkdir(parents=True)
    (sql / "0001_walking_skeleton.sql").write_text("SELECT 1;\n")
    (root / "src/unrelated.sql").write_text("not a migration\n")
    bundle = freeze_controller(root, tmp_path / "controller.tar")
    with tarfile.open(bundle.archive) as tar:
        assert "src/hcuopt/evaluation/protocols/s0-g0-bw20-v1.yaml" in tar.getnames()
        assert "src/credentials.yaml" not in tar.getnames()
        assert "src/hcuopt/storage/sql/0001_walking_skeleton.sql" in tar.getnames()
        assert "src/unrelated.sql" not in tar.getnames()


def test_freeze_rejects_changed_identity_assets(tmp_path):
    root = source(tmp_path)
    (root / "src/hcuopt/deployment/assets/bw20-passwd").write_text("root:x:0:0\n")
    with pytest.raises(ValueError, match="identity assets"):
        freeze_controller(root, tmp_path / "controller.tar")


def test_upload_refuses_tampered_archive_before_contacting_host(tmp_path):
    bundle = freeze_controller(source(tmp_path), tmp_path / "source.tar")
    bundle.archive.write_bytes(b"tampered")
    runner = SimpleNamespace(host="10.17.1.20", user="github", port=22)
    with pytest.raises(ValueError, match="archive changed"):
        stage_controller(plan=plan(), bundle=bundle, runner=runner)


def test_upload_refuses_wrong_host_or_destination(tmp_path):
    bundle = freeze_controller(source(tmp_path), tmp_path / "source.tar")
    runner = SimpleNamespace(host="other", user="github", port=22)
    with pytest.raises(ValueError, match="endpoint"):
        stage_controller(plan=plan(), bundle=bundle, runner=runner)
    runner.host = "10.17.1.20"
    with pytest.raises(ValueError):
        stage_controller(plan=replace(plan(), source_root="/home/github"),
                         bundle=bundle, runner=runner)


def test_host_programs_keep_python36_syntax():
    ast.parse(INIT_UPLOAD, feature_version=(3, 6))
    ast.parse(UNPACK_UPLOAD, feature_version=(3, 6))
