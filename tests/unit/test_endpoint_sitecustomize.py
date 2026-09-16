from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
SOURCE = ROOT / "src" / "hcuopt" / "evaluation" / "endpoint_sitecustomize.py"


def _run(
    tmp_path: Path, *, expected_hash: str | None = None
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    hook = tmp_path / "hook"
    modules = tmp_path / "modules"
    hook.mkdir()
    modules.mkdir()
    shutil.copyfile(SOURCE, hook / "sitecustomize.py")
    target = modules / "target_overlay.py"
    target.write_text("VALUE = 7\n", encoding="utf-8")
    expected_hash = expected_hash or "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
    evidence = tmp_path / "evidence" / "activation.json"
    environment = dict(os.environ)
    environment.update(
        PYTHONPATH=os.pathsep.join((str(hook), str(modules))),
        HCUOPT_ENDPOINT_TARGET_MODULE="target_overlay",
        HCUOPT_ENDPOINT_TARGET_PATH=str(target.resolve()),
        HCUOPT_ENDPOINT_TARGET_SHA256=expected_hash,
        HCUOPT_ENDPOINT_ACTIVATION_PATH=str(evidence.resolve()),
    )
    completed = subprocess.run(
        [sys.executable, "-c", "import target_overlay; print(target_overlay.VALUE)"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    return completed, evidence, target


def test_sitecustomize_attests_the_module_imported_by_the_real_process(tmp_path: Path) -> None:
    completed, evidence, target = _run(tmp_path)
    expected = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()

    assert completed.returncode == 0, completed.stderr
    value = json.loads(evidence.read_text(encoding="utf-8"))
    assert value["schema_version"] == "sglang-endpoint-import-attestation-v1"
    assert value["module_name"] == "target_overlay"
    assert value["module_sha256"] == expected
    assert value["process_id"] > 0


def test_sitecustomize_fails_import_when_module_hash_differs(tmp_path: Path) -> None:
    completed, evidence, _target = _run(
        tmp_path, expected_hash="sha256:" + "0" * 64
    )

    assert completed.returncode != 0
    assert "differs from the frozen Hash" in completed.stderr
    assert not evidence.exists()
