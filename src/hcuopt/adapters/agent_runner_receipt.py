# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from hcuopt.adapters.agent_runner import AgentRunResult
from hcuopt.contracts.agent_runner_v1 import (
    RunnerExecutionReceipt,
    RunnerExecutionReceiptRef,
    RunnerExecutionRecord,
    runner_execution_receipt_hash,
    runner_execution_receipt_id_for,
)
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.source_hash import file_uri_to_path

MAX_RUNNER_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_RUNNER_RAW_OUTPUT_BYTES = 100 * 1024 * 1024


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _digest_path(root: Path, namespace: str, digest: str, filename: str) -> Path:
    value = digest.removeprefix("sha256:")
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise SourceArtifactError("Runner Receipt Store received an invalid SHA256 identity")
    return root / namespace / "sha256" / value[:2] / value[2:] / filename


def _read_regular(path: Path, *, maximum_bytes: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise SourceArtifactError("Runner Receipt Store entry is not a regular file")
    if path.stat().st_size > maximum_bytes:
        raise SourceArtifactError("Runner Receipt Store entry exceeds its size limit")
    return path.read_bytes()


def _publish_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not hmac.compare_digest(_read_regular(path, maximum_bytes=len(payload)), payload):
            raise SourceArtifactError(
                "Runner Receipt Store immutable identity contains different bytes"
            )
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
            if not hmac.compare_digest(
                _read_regular(path, maximum_bytes=len(payload)), payload
            ):
                raise SourceArtifactError(
                    "Runner Receipt Store concurrent publication changed immutable bytes"
                ) from error
    finally:
        temporary.unlink(missing_ok=True)


class RunnerExecutionReceiptStore:
    """Publish and independently reread one bounded Runner execution."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def publish(self, result: AgentRunResult) -> RunnerExecutionReceiptRef:
        try:
            execution = RunnerExecutionRecord.model_validate(
                result.evidence.model_dump(mode="json")
            )
        except ValidationError as error:
            raise SourceArtifactError(
                "Runner Receipt Store rejected an invalid Execution Record"
            ) from error
        raw_output_uri = None
        raw_output_hash = None
        raw_output_bytes = 0
        if execution.status == "succeeded":
            if result.proposal_bytes is None:
                raise SourceArtifactError("successful Runner result omitted its proposal bytes")
            raw_output = result.proposal_bytes
            if len(raw_output) > MAX_RUNNER_RAW_OUTPUT_BYTES:
                raise SourceArtifactError("Runner raw output exceeds its Store size limit")
            raw_output_hash = _sha256(raw_output)
            if (
                raw_output_hash != execution.stdout_hash
                or len(raw_output) != execution.stdout_bytes_consumed
            ):
                raise SourceArtifactError("Runner raw output differs from its execution record")
            raw_path = _digest_path(
                self.root,
                "raw-output",
                raw_output_hash,
                "output.bin",
            )
            _publish_once(raw_path, raw_output)
            raw_output_uri = raw_path.resolve(strict=True).as_uri()
            raw_output_bytes = len(raw_output)
        elif result.proposal_bytes is not None:
            raise SourceArtifactError("unsuccessful Runner result exposed proposal bytes")

        receipt = RunnerExecutionReceipt(
            receipt_id=runner_execution_receipt_id_for(execution.attempt_id),
            execution=execution,
            raw_output_uri=raw_output_uri,
            raw_output_hash=raw_output_hash,
            raw_output_bytes=raw_output_bytes,
        )
        content_hash = runner_execution_receipt_hash(receipt)
        payload = canonical_json_bytes(receipt)
        if len(payload) > MAX_RUNNER_RECEIPT_BYTES:
            raise SourceArtifactError("Runner Receipt exceeds its Store size limit")
        path = _digest_path(self.root, "receipts", content_hash, "receipt.json")
        _publish_once(path, payload)
        reference = RunnerExecutionReceiptRef(
            receipt_id=receipt.receipt_id,
            attempt_id=execution.attempt_id,
            generation_run_id=execution.generation_run_id,
            request_id=execution.request_id,
            request_hash=execution.request_hash,
            uri=path.resolve(strict=True).as_uri(),
            content_hash=content_hash,
        )
        self.load(reference)
        return reference

    def load(self, reference: RunnerExecutionReceiptRef) -> RunnerExecutionReceipt:
        expected_path = _digest_path(
            self.root,
            "receipts",
            reference.content_hash,
            "receipt.json",
        ).resolve(strict=True)
        actual_path = file_uri_to_path(reference.uri).resolve(strict=True)
        if actual_path != expected_path:
            raise SourceArtifactError(
                "Runner Receipt URI is outside its content-addressed identity"
            )
        payload = _read_regular(actual_path, maximum_bytes=MAX_RUNNER_RECEIPT_BYTES)
        receipt = RunnerExecutionReceipt.model_validate_json(payload)
        if runner_execution_receipt_hash(receipt) != reference.content_hash:
            raise SourceArtifactError("Runner Receipt content Hash changed in Store")
        execution = receipt.execution
        if (
            receipt.receipt_id != reference.receipt_id
            or execution.attempt_id != reference.attempt_id
            or execution.generation_run_id != reference.generation_run_id
            or execution.request_id != reference.request_id
            or execution.request_hash != reference.request_hash
        ):
            raise SourceArtifactError("Runner Receipt Ref identity differs from stored content")
        if receipt.raw_output_uri is not None:
            assert receipt.raw_output_hash is not None
            expected_raw = _digest_path(
                self.root,
                "raw-output",
                receipt.raw_output_hash,
                "output.bin",
            ).resolve(strict=True)
            actual_raw = file_uri_to_path(receipt.raw_output_uri).resolve(strict=True)
            if actual_raw != expected_raw:
                raise SourceArtifactError("Runner raw output URI escaped its Store identity")
            raw_output = _read_regular(
                actual_raw,
                maximum_bytes=MAX_RUNNER_RAW_OUTPUT_BYTES,
            )
            if (
                _sha256(raw_output) != receipt.raw_output_hash
                or len(raw_output) != receipt.raw_output_bytes
            ):
                raise SourceArtifactError("Runner raw output changed after publication")
        return receipt


__all__ = ["RunnerExecutionReceiptStore"]
