# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Remote host validation must survive PYTHONOPTIMIZE or Python -O."""

import ast
import hashlib
import inspect
import json
import subprocess
import sys

import pytest

from hcuopt.deployment import bw20_build_transport, bw20_pair_bridge


def remote_scripts(module):
    tree = ast.parse(inspect.getsource(module))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value.lstrip().startswith("import ")]


@pytest.mark.parametrize("module", [bw20_build_transport, bw20_pair_bridge])
def test_remote_safety_programs_have_no_optimized_away_assertions(module):
    scripts = remote_scripts(module)
    assert scripts
    for script in scripts:
        parsed = ast.parse(script, feature_version=(3, 6))
        assert not any(isinstance(node, ast.Assert) for node in ast.walk(parsed))


def test_inventory_rejects_nonregular_member_under_optimization(tmp_path):
    (tmp_path / "unexpected-directory").mkdir()
    result = subprocess.run(
        [sys.executable, "-I", "-O", "-c", bw20_pair_bridge.INVENTORY, str(tmp_path)],
        capture_output=True, timeout=10,
    )
    assert result.returncode != 0
    assert b"nonregular inventory member" in result.stderr


@pytest.mark.parametrize("tamper", ["manifest", "member"])
def test_controller_tampering_rejected_under_optimization(tmp_path, tamper):
    code = next(s for s in remote_scripts(bw20_build_transport)
                if "controller-manifest.json" in s)
    member = tmp_path / "module.py"
    member.write_bytes(b"original")
    manifest = tmp_path / "controller-manifest.json"
    manifest.write_text(json.dumps({member.name: hashlib.sha256(b"original").hexdigest()}))
    pinned = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    if tamper == "manifest":
        manifest.write_text("{}")
    else:
        member.write_bytes(b"tampered")
    result = subprocess.run(
        [sys.executable, "-I", "-O", "-c", code, str(tmp_path), pinned],
        capture_output=True, timeout=10,
    )
    assert result.returncode != 0
    assert b"hash mismatch" in result.stderr
