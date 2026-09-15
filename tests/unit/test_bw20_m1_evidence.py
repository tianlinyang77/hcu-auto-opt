# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from hcuopt.deployment import bw20_m1_evidence as evidence
from hcuopt.deployment.bw20_m1_runtime import build_m1_container_plan
from hcuopt.source_hash import file_uri_to_path
from hcuopt.targets import load_target

ROOT = Path(__file__).resolve().parents[2]
RUN_ID = UUID("00000000-0000-0000-0000-000000000125")
PAYLOAD = b'{"schema_version":"fixture"}\n'
SHA256 = "sha256:" + hashlib.sha256(PAYLOAD).hexdigest()


def _target():
    return load_target(ROOT / "config/targets/bw20-sglang-0.5.12.yaml")


class _Transport:
    changed = False

    def __init__(self, runner):
        self.runner = runner
        self.reads = 0

    def checked(self, argv, timeout=60):
        del timeout
        self.reads += 1
        return json.dumps(
            {
                "schema_version": "bw20-m1-evidence-read-v1",
                "path": argv[-1],
                "size": len(PAYLOAD),
                "sha256": SHA256,
                "inode": 10 + (self.reads if self.changed else 0),
                "device": 20,
                "mtime_ns": 30,
            }
        ).encode()

    def copy(self, source, destination, *, download=False):
        del source
        assert download is True
        Path(destination).write_bytes(PAYLOAD)


@pytest.fixture(autouse=True)
def _reset():
    _Transport.changed = False


def _mirror(tmp_path, monkeypatch):
    plan = build_m1_container_plan(
        _target(),
        run_id=RUN_ID,
        arm="baseline",
        acquisition_ordinal=0,
        fencing_token=9,
    )
    monkeypatch.setattr(evidence, "BW20Transport", _Transport)
    return evidence.BW20M1EvidenceMirror(
        plan=plan,
        runner=SimpleNamespace(),
        local_root=tmp_path / "mirror",
    )


def test_host_evidence_reader_remains_python36_compatible() -> None:
    assert evidence.host_script_is_python36_compatible()


def test_mirror_downloads_one_hash_bound_allowlisted_file(tmp_path, monkeypatch) -> None:
    mirror = _mirror(tmp_path, monkeypatch)
    reference = mirror.fetch("cache-namespace.json", SHA256)
    assert reference.sha256 == SHA256
    assert file_uri_to_path(reference.uri).read_bytes() == PAYLOAD
    assert len(mirror.observations) == 1
    assert mirror.transport.reads == 2


@pytest.mark.parametrize("name", ["../secret", "stdout.log", "device-event-1.json"])
def test_mirror_rejects_names_outside_evidence_allowlist(tmp_path, monkeypatch, name) -> None:
    mirror = _mirror(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="allowlist"):
        mirror.fetch(name, SHA256)
    assert mirror.transport.reads == 0


def test_mirror_rejects_remote_change_across_download(tmp_path, monkeypatch) -> None:
    mirror = _mirror(tmp_path, monkeypatch)
    mirror.transport.changed = True
    with pytest.raises(RuntimeError, match="changed during download"):
        mirror.fetch("device-event-0000.json", SHA256)
