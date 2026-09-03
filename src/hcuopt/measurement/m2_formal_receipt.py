# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hmac
import json
import os
import tempfile
from pathlib import Path

from hcuopt.contracts.m2_formal_execution_v1 import (
    M2FormalPhaseExecutionReceipt,
    M2FormalPhaseExecutionReceiptRef,
    M2FormalPhaseExecutionRecord,
    m2_formal_execution_receipt_hash,
    m2_formal_execution_receipt_id_for,
)
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.source_hash import file_uri_to_path

MAX_FORMAL_EXECUTION_RECEIPT_BYTES = 2 * 1024 * 1024


def _digest_path(root: Path, digest: str) -> Path:
    value = digest.removeprefix("sha256:")
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise SourceArtifactError("Formal Receipt Store received an invalid SHA256 identity")
    return root / "receipts" / "sha256" / value[:2] / value[2:] / "receipt.json"


def _binding_path(root: Path, receipt_id: object) -> Path:
    return root / "receipt-bindings" / f"{receipt_id}.json"


def _is_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction is not None and is_junction())


def _prepare_no_follow_directory(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    chain = tuple(reversed(absolute.parents)) + (absolute,)
    for directory in chain:
        if directory.exists():
            if _is_link(directory) or not directory.is_dir():
                raise SourceArtifactError(
                    "Formal evidence Store directory contains a link or non-directory"
                )
        else:
            try:
                directory.mkdir()
            except FileExistsError:
                pass
            if _is_link(directory) or not directory.is_dir():
                raise SourceArtifactError(
                    "Formal evidence Store directory was concurrently redirected"
                )
    return absolute


def _require_contained(root: Path, path: Path) -> Path:
    absolute_root = Path(os.path.abspath(root))
    absolute_path = Path(os.path.abspath(path))
    try:
        relative = absolute_path.relative_to(absolute_root)
    except ValueError as error:
        raise SourceArtifactError("Formal evidence path escaped its Store root") from error
    cursor = absolute_root
    if _is_link(cursor) or not cursor.is_dir():
        raise SourceArtifactError("Formal evidence Store root is not a regular directory")
    for part in relative.parts[:-1]:
        cursor = cursor / part
        if _is_link(cursor) or not cursor.is_dir():
            raise SourceArtifactError("Formal evidence parent directory is not trusted")
    if _is_link(absolute_path):
        raise SourceArtifactError("Formal evidence file cannot be a link")
    return absolute_path


def _read_regular(root: Path, path: Path, maximum_bytes: int) -> bytes:
    path = _require_contained(root, path)
    if path.is_symlink() or not path.is_file():
        raise SourceArtifactError("Formal Receipt Store entry is not a regular file")
    if path.stat().st_size > maximum_bytes:
        raise SourceArtifactError("Formal Receipt Store entry exceeds its size limit")
    return path.read_bytes()


def _publish_once(root: Path, path: Path, payload: bytes) -> None:
    root = _prepare_no_follow_directory(root)
    path = Path(os.path.abspath(path))
    try:
        path.relative_to(root)
    except ValueError as error:
        raise SourceArtifactError("Formal evidence publication escaped its Store root") from error
    _prepare_no_follow_directory(path.parent)
    _require_contained(root, path)
    if path.exists():
        if not hmac.compare_digest(_read_regular(root, path, len(payload)), payload):
            raise SourceArtifactError("Formal Receipt immutable identity already has other bytes")
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            if not hmac.compare_digest(_read_regular(root, path, len(payload)), payload):
                raise SourceArtifactError(
                    "Formal Receipt concurrent publication changed immutable bytes"
                ) from error
    finally:
        temporary.unlink(missing_ok=True)


class M2FormalPhaseExecutionReceiptStore:
    """Content-addressed store with a write-once Receipt-ID binding."""

    def __init__(self, root: Path) -> None:
        self.root = _prepare_no_follow_directory(root)

    def publish(self, record: M2FormalPhaseExecutionRecord) -> M2FormalPhaseExecutionReceiptRef:
        receipt = M2FormalPhaseExecutionReceipt(
            receipt_id=m2_formal_execution_receipt_id_for(record.execution_id),
            execution=record,
            created_at=record.finished_at,
        )
        content_hash = m2_formal_execution_receipt_hash(receipt)
        payload = canonical_json_bytes(receipt)
        if len(payload) > MAX_FORMAL_EXECUTION_RECEIPT_BYTES:
            raise SourceArtifactError("Formal execution Receipt exceeds its size limit")
        path = _digest_path(self.root, content_hash)
        binding = canonical_json_bytes(
            {
                "schema_version": "m2a-formal-phase-execution-receipt-binding-v1",
                "receipt_id": str(receipt.receipt_id),
                "content_hash": content_hash,
            }
        )
        binding_path = _binding_path(self.root, receipt.receipt_id)
        if binding_path.exists() and not hmac.compare_digest(
            _read_regular(self.root, binding_path, 4096), binding
        ):
            raise SourceArtifactError("Formal Receipt immutable identity already has other bytes")
        _publish_once(self.root, path, payload)
        _publish_once(self.root, binding_path, binding)
        reference = M2FormalPhaseExecutionReceiptRef(
            receipt_id=receipt.receipt_id,
            execution_id=record.execution_id,
            round_id=record.binding.round_id,
            candidate_id=record.binding.candidate_id,
            phase=record.binding.phase,
            uri=path.absolute().as_uri(),
            content_hash=content_hash,
        )
        self.load(reference)
        return reference

    def load(self, reference: M2FormalPhaseExecutionReceiptRef) -> M2FormalPhaseExecutionReceipt:
        expected = _digest_path(self.root, reference.content_hash).absolute()
        actual = file_uri_to_path(reference.uri).absolute()
        if actual != expected:
            raise SourceArtifactError("Formal Receipt URI escaped its content identity")
        payload = _read_regular(self.root, actual, MAX_FORMAL_EXECUTION_RECEIPT_BYTES)
        receipt = M2FormalPhaseExecutionReceipt.model_validate_json(payload)
        if m2_formal_execution_receipt_hash(receipt) != reference.content_hash:
            raise SourceArtifactError("Formal execution Receipt content Hash changed")
        execution = receipt.execution
        if (
            receipt.receipt_id != reference.receipt_id
            or execution.execution_id != reference.execution_id
            or execution.binding.round_id != reference.round_id
            or execution.binding.candidate_id != reference.candidate_id
            or execution.binding.phase is not reference.phase
        ):
            raise SourceArtifactError("Formal execution Receipt Ref binding differs")
        binding_payload = _read_regular(
            self.root, _binding_path(self.root, receipt.receipt_id), 4096
        )
        binding = json.loads(binding_payload)
        if (
            binding.get("receipt_id") != str(receipt.receipt_id)
            or binding.get("content_hash") != reference.content_hash
        ):
            raise SourceArtifactError("Formal Receipt ID was rebound to other content")
        return receipt


__all__ = ["M2FormalPhaseExecutionReceiptStore"]
