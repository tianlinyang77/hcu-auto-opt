from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from dcuopt.contracts.platform_v1 import TargetSpec
from dcuopt.domain.errors import TargetConfigError

TARGET_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def load_target(path: Path) -> TargetSpec:
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TargetConfigError(f"cannot read target lock {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise TargetConfigError(f"invalid YAML in target lock {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise TargetConfigError(f"target lock {path} must contain one mapping")
    try:
        return TargetSpec.model_validate(raw)
    except ValidationError as exc:
        raise TargetConfigError(f"target lock {path} violates platform-v1: {exc}") from exc


class TargetCatalog:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def load(self, target_id: str) -> TargetSpec:
        if TARGET_ID_PATTERN.fullmatch(target_id) is None:
            raise TargetConfigError(f"invalid target id: {target_id!r}")
        matches = [
            path
            for suffix in (".yaml", ".yml")
            if (path := self.root / f"{target_id}{suffix}").is_file()
        ]
        if not matches:
            raise TargetConfigError(f"target not found in {self.root}: {target_id}")
        if len(matches) > 1:
            raise TargetConfigError(f"target has both .yaml and .yml definitions: {target_id}")
        target = load_target(matches[0])
        if target.target_id != target_id:
            raise TargetConfigError(
                f"target id mismatch: requested {target_id}, file declares {target.target_id}"
            )
        return target
