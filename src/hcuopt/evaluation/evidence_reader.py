# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from hcuopt.measurement.evidence import canonical_json_bytes

MAX_RAW_EVIDENCE_BYTES = 64 * 1024 * 1024
MAX_RAW_JSON_DEPTH = 64
_WINDOWS_DRIVE_PATH = re.compile(r"^/[A-Za-z]:/")


class EvidenceReadError(ValueError):
    """Raw evidence is corrupt, unsafe, or outside the trusted evidence root."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class HashedEvidenceReader:
    """Read bounded, hashed evidence without following links outside an allowed root."""

    def __init__(self, allowed_root: Path, *, max_bytes: int = MAX_RAW_EVIDENCE_BYTES) -> None:
        self.allowed_root = allowed_root.resolve(strict=True)
        if not self.allowed_root.is_dir():
            raise ValueError("allowed evidence root must be a directory")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self.max_bytes = max_bytes

    def read(self, uri: str, expected_hash: str) -> dict[str, Any]:
        _, value = self._read_document(uri, expected_hash)
        return value

    def read_bytes(self, uri: str, expected_hash: str) -> bytes:
        """Return verified canonical JSON bytes for strict Pydantic JSON validation."""

        encoded, _ = self._read_document(uri, expected_hash)
        return encoded

    def read_raw_bytes(self, uri: str, expected_hash: str) -> bytes:
        """Return bounded, securely opened bytes after verifying their SHA-256."""

        path = self._resolve_file_uri(uri)
        encoded = self._secure_read(path)
        actual_hash = "sha256:" + hashlib.sha256(encoded).hexdigest()
        if actual_hash != expected_hash:
            raise EvidenceReadError("evidence_hash_mismatch", f"SHA-256 mismatch: {path}")
        return encoded

    def _read_document(self, uri: str, expected_hash: str) -> tuple[bytes, dict[str, Any]]:
        path = self._resolve_file_uri(uri)
        encoded = self._secure_read(path)
        actual_hash = "sha256:" + hashlib.sha256(encoded).hexdigest()
        if actual_hash != expected_hash:
            raise EvidenceReadError("evidence_hash_mismatch", f"SHA-256 mismatch: {path}")
        try:
            text = encoded.decode("utf-8", errors="strict")
            value = json.loads(text, parse_constant=self._reject_json_constant)
        except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise EvidenceReadError("evidence_invalid_json", f"invalid JSON: {path}") from exc
        if not isinstance(value, dict):
            raise EvidenceReadError("evidence_invalid_json", "raw evidence root must be an object")
        _validate_json_depth(value, maximum_depth=MAX_RAW_JSON_DEPTH)
        try:
            canonical = canonical_json_bytes(value)
        except (TypeError, ValueError, RecursionError) as exc:
            raise EvidenceReadError(
                "evidence_noncanonical", "raw evidence is not canonical"
            ) from exc
        if encoded != canonical:
            raise EvidenceReadError(
                "evidence_noncanonical",
                "raw evidence bytes do not match canonical JSON representation",
            )
        return encoded, value

    def _resolve_file_uri(self, uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise EvidenceReadError(
                "evidence_uri_invalid", "only local file: evidence URIs are allowed"
            )
        raw_path = unquote(parsed.path)
        if os.name == "nt" and _WINDOWS_DRIVE_PATH.match(raw_path):
            raw_path = raw_path[1:]
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            raise EvidenceReadError("evidence_uri_invalid", "evidence URI path must be absolute")
        if ".." in candidate.parts:
            raise EvidenceReadError("evidence_path_escape", "evidence path contains traversal")
        try:
            candidate.relative_to(self.allowed_root)
            self._reject_symlinks(candidate)
            candidate.resolve(strict=True).relative_to(self.allowed_root)
        except EvidenceReadError:
            raise
        except (OSError, ValueError) as exc:
            raise EvidenceReadError(
                "evidence_path_escape", "evidence path escapes allowed root"
            ) from exc
        return candidate

    def _reject_symlinks(self, candidate: Path) -> None:
        relative = candidate.relative_to(self.allowed_root)
        cursor = self.allowed_root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise EvidenceReadError(
                    "evidence_symlink", f"symbolic link is not allowed: {cursor}"
                )

    def _secure_read(self, path: Path) -> bytes:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise EvidenceReadError(
                "evidence_platform_unsupported",
                "Formal evidence requires POSIX openat/O_NOFOLLOW path semantics",
            )
        return self._secure_read_posix(path)

    def _secure_read_posix(self, path: Path) -> bytes:
        relative = path.relative_to(self.allowed_root)
        if not relative.parts:
            raise EvidenceReadError("evidence_not_regular", f"not a regular file: {path}")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_descriptors: list[int] = []
        file_descriptor: int | None = None
        try:
            current = os.open(self.allowed_root, directory_flags)
            directory_descriptors.append(current)
            for part in relative.parts[:-1]:
                current = os.open(part, directory_flags, dir_fd=current)
                directory_descriptors.append(current)
            file_descriptor = os.open(
                relative.parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                dir_fd=current,
            )
            encoded = self._read_bounded_descriptor(file_descriptor, path)
            opened = os.fstat(file_descriptor)
            current_path = os.stat(relative.parts[-1], dir_fd=current, follow_symlinks=False)
            if _is_reparse_point(current_path) or not _same_file_identity(opened, current_path):
                raise EvidenceReadError(
                    "evidence_changed_during_read",
                    "evidence path identity changed while it was being read",
                )
            return encoded
        except OSError as exc:
            raise EvidenceReadError(
                "evidence_unreadable", f"cannot securely open evidence: {path}"
            ) from exc
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            for descriptor in reversed(directory_descriptors):
                os.close(descriptor)

    def _read_bounded_descriptor(self, descriptor: int, path: Path) -> bytes:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise EvidenceReadError("evidence_not_regular", f"not a regular file: {path}")
        if opened.st_size > self.max_bytes:
            raise EvidenceReadError("evidence_too_large", f"evidence exceeds size limit: {path}")
        chunks: list[bytes] = []
        remaining = self.max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > self.max_bytes:
            raise EvidenceReadError("evidence_too_large", f"evidence exceeds size limit: {path}")
        finished = os.fstat(descriptor)
        if not _same_file_identity(opened, finished) or finished.st_size != len(encoded):
            raise EvidenceReadError(
                "evidence_changed_during_read", "evidence changed while it was being read"
            )
        return encoded

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_attribute)


def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _validate_json_depth(value: object, *, maximum_depth: int) -> None:
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > maximum_depth:
            raise EvidenceReadError(
                "evidence_invalid_json",
                f"raw evidence exceeds maximum JSON depth {maximum_depth}",
            )
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
