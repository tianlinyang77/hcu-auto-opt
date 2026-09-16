"""Standalone sitecustomize payload for SGLang Overlay import attestation.

The deployment stages this file as ``sitecustomize.py`` on an exact, read-only
PYTHONPATH.  It uses only the Python standard library and writes no verdict.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType
from typing import Any

SCHEMA_VERSION = "sglang-endpoint-import-attestation-v1"
_NAMES = (
    "HCUOPT_ENDPOINT_TARGET_MODULE",
    "HCUOPT_ENDPOINT_TARGET_PATH",
    "HCUOPT_ENDPOINT_TARGET_SHA256",
    "HCUOPT_ENDPOINT_ACTIVATION_PATH",
)


def _configuration() -> tuple[str, Path, str, Path] | None:
    values = tuple(os.environ.get(name) for name in _NAMES)
    if not any(values):
        return None
    if not all(values):
        raise RuntimeError("incomplete HCUOPT endpoint activation configuration")
    module_name, target_path, expected_hash, output_path = values
    assert module_name and target_path and expected_hash and output_path
    if expected_hash[:7] != "sha256:" or len(expected_hash) != 71:
        raise RuntimeError("invalid HCUOPT endpoint activation SHA256")
    if any(character not in "0123456789abcdef" for character in expected_hash[7:]):
        raise RuntimeError("invalid HCUOPT endpoint activation SHA256")
    target = Path(target_path)
    output = Path(output_path)
    if not target.is_absolute() or not output.is_absolute():
        raise RuntimeError("HCUOPT endpoint activation paths must be absolute")
    return module_name, target, expected_hash, output


_CONFIGURATION = _configuration()
_ORIGINAL_IMPORT = builtins.__import__
_OBSERVING = False
_OBSERVED = False


def _audited_import(
    name: str,
    globals: dict[str, Any] | None = None,
    locals: dict[str, Any] | None = None,
    fromlist: tuple[str, ...] | list[str] = (),
    level: int = 0,
) -> ModuleType:
    global _OBSERVING
    imported = _ORIGINAL_IMPORT(name, globals, locals, fromlist, level)
    if _CONFIGURATION is None or _OBSERVED or _OBSERVING:
        return imported
    target_name = _CONFIGURATION[0]
    if target_name not in sys.modules:
        return imported
    _OBSERVING = True
    try:
        _observe(sys.modules[target_name])
    finally:
        _OBSERVING = False
    return imported


def _observe(module: ModuleType) -> None:
    global _OBSERVED
    assert _CONFIGURATION is not None
    module_name, expected_path, expected_hash, output_path = _CONFIGURATION
    raw_path = getattr(module, "__file__", None)
    if not isinstance(raw_path, str):
        raise RuntimeError("target endpoint module has no file identity")
    module_path = Path(raw_path)
    before = module_path.lstat()
    if (
        module_path != expected_path
        or module_path.resolve(strict=True) != expected_path
        or not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > 16 * 1024 * 1024
    ):
        raise RuntimeError("target endpoint module path is redirected or unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    digest = hashlib.sha256()
    with os.fdopen(os.open(module_path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError("target endpoint module changed during open")
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        after = os.fstat(stream.fileno())
    actual_hash = "sha256:" + digest.hexdigest()
    if actual_hash != expected_hash:
        raise RuntimeError("target endpoint module differs from the frozen Hash")
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise RuntimeError("target endpoint module changed during read")
    value = {
        "schema_version": SCHEMA_VERSION,
        "module_name": module_name,
        "module_path": str(module_path),
        "module_sha256": actual_hash,
        "process_id": os.getpid(),
        "parent_process_id": os.getppid(),
        "captured_monotonic_ns": time.monotonic_ns(),
        "device": opened.st_dev,
        "inode": opened.st_ino,
        "size": opened.st_size,
        "mtime_ns": after.st_mtime_ns,
    }
    _publish(output_path, value)
    _OBSERVED = True


def _publish(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, allow_nan=False, sort_keys=True).encode("utf-8") + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=".activation-", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != encoded:
                raise RuntimeError("endpoint activation evidence already differs") from None
        else:
            temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)


if _CONFIGURATION is not None:
    builtins.__import__ = _audited_import
