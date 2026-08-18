from __future__ import annotations

import subprocess
from pathlib import Path

from hcuopt.contracts.platform_v1 import TargetSpec
from hcuopt.targets.loader import load_target

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def create_repository(path: Path) -> tuple[Path, str]:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    git(path, "config", "user.name", "F1-C Test")
    git(path, "config", "user.email", "f1-c@example.invalid")
    (path / "README.md").write_text("baseline\n", encoding="utf-8")
    executable = path / "run.sh"
    executable.write_text("#!/bin/sh\necho stable\n", encoding="utf-8")
    executable.chmod(0o755)
    (path / "link").symlink_to("README.md")
    git(path, "add", ".")
    git(path, "commit", "-m", "baseline")
    return path, git(path, "rev-parse", "HEAD")


def target_for(repository: Path, checkout: Path, commit: str) -> TargetSpec:
    target = load_target(PROJECT_ROOT / "config/targets/nmz36-sglang-0.5.12.yaml")
    source = target.source_baseline.model_copy(
        update={
            "repository": str(repository),
            "branch": "main",
            "commit": commit,
            "branch_head_observed_at_lock": commit,
            "clean_checkout": str(checkout),
        }
    )
    return target.model_copy(update={"source_baseline": source})
