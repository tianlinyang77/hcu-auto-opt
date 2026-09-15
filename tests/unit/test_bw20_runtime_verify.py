# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import hashlib
import json
import os
from pathlib import Path

import pytest

from hcuopt.deployment.bw20_runtime_verify import (
    _pinned,
    checked_digest,
    verify_model_inventory,
    verify_preparation,
)


@pytest.fixture
def inventory(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_bytes(b"{}")
    (model / "weights.bin").write_bytes(b"weights")
    return model, {
        p.name: {"size": p.stat().st_size, "sha256": checked_digest(p)}
        for p in model.iterdir()
    }


def test_model_inventory_is_read_only(inventory):
    model, entries = inventory
    before = {p.name: p.read_bytes() for p in model.iterdir()}
    assert verify_model_inventory(model, entries) == {"file_count": 2, "total_bytes": 9}
    assert before == {p.name: p.read_bytes() for p in model.iterdir()}


@pytest.mark.parametrize("mutation", ["changed", "extra", "missing", "size", "empty"])
def test_model_inventory_rejects_drift(inventory, mutation):
    model, entries = inventory
    if mutation == "changed":
        (model / "weights.bin").write_bytes(b"WEIGHTS")
    elif mutation == "extra":
        (model / "untracked.bin").write_bytes(b"new")
    elif mutation == "missing":
        (model / "weights.bin").unlink()
    elif mutation == "size":
        entries["weights.bin"]["size"] = 8
    else:
        entries.clear()
    with pytest.raises(ValueError):
        verify_model_inventory(model, entries)


@pytest.mark.parametrize("size", [True, -1, 16 * 1024**3 + 1, "7"])
def test_invalid_sizes_refused_before_read(inventory, size):
    model, entries = inventory
    entries["weights.bin"]["size"] = size
    with pytest.raises(ValueError, match="size"):
        verify_model_inventory(model, entries)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs Windows privilege")
@pytest.mark.parametrize("directory", [False, True])
def test_symlink_model_members_refused(inventory, directory):
    model, entries = inventory
    if directory:
        (model / "redirect").symlink_to(model.parent, target_is_directory=True)
    else:
        (model / "alias.bin").symlink_to(model / "weights.bin")
        entries["alias.bin"] = entries["weights.bin"]
    with pytest.raises(ValueError, match="redirected|unsafe"):
        verify_model_inventory(model, entries)


def test_pins_must_be_independent_and_valid(tmp_path):
    path = tmp_path / "input.json"
    path.write_bytes(b"{}")
    pin = "sha256:" + hashlib.sha256(b"{}").hexdigest()
    assert _pinned(path, pin) == b"{}"
    with pytest.raises(ValueError, match="pin"):
        _pinned(path, "sha256:" + "0" * 64)
    with pytest.raises(ValueError, match="pin"):
        _pinned(path, "")
    path.write_bytes(b"[]")
    with pytest.raises(ValueError, match="pin"):
        _pinned(path, pin)


def test_digest_refuses_directory_and_budget_overrun(tmp_path):
    with pytest.raises(ValueError, match="unsafe"):
        checked_digest(tmp_path)
    path = tmp_path / "large"
    path.write_bytes(b"123")
    with pytest.raises(ValueError, match="oversized"):
        checked_digest(path, limit=2)


def test_digest_detects_file_replaced_after_read(tmp_path, monkeypatch):
    path = tmp_path / "input"
    path.write_bytes(b"before")
    original = Path.lstat
    count = 0

    def changed(current):
        nonlocal count
        if current == path:
            count += 1
            if count == 2:
                path.write_bytes(b"different size")
        return original(current)

    monkeypatch.setattr(Path, "lstat", changed)
    with pytest.raises(ValueError, match="changed"):
        checked_digest(path)


@pytest.mark.parametrize("mutation", ["schema", "input_set", "receipt_bytes"])
def test_bad_preparation_never_reaches_runner(tmp_path, mutation):
    record = {"schema_version": "bw20-runtime-preparation-v1", "input_hashes": {}}
    if mutation == "schema":
        record["schema_version"] = "other"
    path = tmp_path / "preparation.json"
    path.write_text(json.dumps(record))
    pin = checked_digest(path)
    if mutation == "receipt_bytes":
        path.write_text("{}")
    with pytest.raises(ValueError):
        verify_preparation(root=tmp_path, controller=tmp_path,
                           preparation_sha256=pin, runner=None)
