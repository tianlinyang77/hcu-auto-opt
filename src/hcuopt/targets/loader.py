from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from hcuopt.contracts.platform_v1 import TargetSpec
from hcuopt.domain.errors import TargetConfigError

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
        raise TargetConfigError(f"target lock {path} violates platform-v1.1: {exc}") from exc


class TargetCatalog:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def source_path(self, target_id: str) -> Path:
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
        return matches[0]

    def load(self, target_id: str) -> TargetSpec:
        target = load_target(self.source_path(target_id))
        if target.target_id != target_id:
            raise TargetConfigError(
                f"target id mismatch: requested {target_id}, file declares {target.target_id}"
            )
        return target

    def list(self) -> list[TargetSpec]:
        if not self.root.is_dir():
            raise TargetConfigError(f"target catalog directory not found: {self.root}")
        targets: dict[str, TargetSpec] = {}
        source_paths: dict[str, Path] = {}
        for path in sorted((*self.root.glob("*.yaml"), *self.root.glob("*.yml"))):
            if path.parent.resolve() != self.root:
                raise TargetConfigError(f"target path escapes catalog root: {path}")
            target = load_target(path)
            if path.stem != target.target_id:
                raise TargetConfigError(
                    f"target id mismatch: file {path.name} declares {target.target_id}"
                )
            previous = source_paths.get(target.target_id)
            if previous is not None:
                raise TargetConfigError(
                    f"duplicate target id {target.target_id}: {previous.name}, {path.name}"
                )
            targets[target.target_id] = target
            source_paths[target.target_id] = path
        return [targets[target_id] for target_id in sorted(targets)]
