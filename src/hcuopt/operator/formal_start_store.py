# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, TypeVar
from uuid import UUID

from pydantic import ValidationError

from hcuopt.contracts.m2_formal_operator_v1 import FormalRoundPlanPreviewView
from hcuopt.contracts.m2_formal_start_v1 import (
    FormalEvaluationStartAuthority,
    FormalExecutionStartAuthority,
)
from hcuopt.domain.errors import NotFound, SourceArtifactError
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.measurement.m2_formal_receipt import (
    _is_link,
    _prepare_no_follow_directory,
    _publish_once,
    _read_regular,
)
from hcuopt.operator.formal_plans import formal_operator_resolved_plan_hash

MAX_FORMAL_START_AUTHORITY_BYTES = 512 * 1024


class DeploymentFormalStartPreviewStore(Protocol):
    """The existing deployment Preview Store; this Store never duplicates it."""

    def load_preview(self, preview_id: UUID) -> FormalRoundPlanPreviewView: ...


class FileFormalStartPreviewStore:
    """Administrator-owned immutable Preview snapshots, not a public upload API.

    Expired previews remain readable for audit; coordinator checks expiration
    and revalidates live authority before use. Storage does not grant execution.
    """

    def __init__(self, root: Path) -> None:
        self.root = _prepare_no_follow_directory(root)

    def _path(self, preview_id: UUID) -> Path:
        # UUID parsing prevents a caller-controlled ID from becoming a path.
        return self.root / "previews" / str(UUID(str(preview_id))) / "preview.json"

    @staticmethod
    def _validate(value: FormalRoundPlanPreviewView) -> FormalRoundPlanPreviewView:
        parsed = FormalRoundPlanPreviewView.model_validate(value.model_dump(mode="json"))
        if formal_operator_resolved_plan_hash(parsed.resolved_plan) != parsed.resolved_plan_hash:
            raise SourceArtifactError("Formal Preview plan Hash does not match content")
        return parsed

    def publish_preview(self, preview: FormalRoundPlanPreviewView) -> FormalRoundPlanPreviewView:
        value = self._validate(preview)
        payload = canonical_json_bytes(value)
        if len(payload) > MAX_FORMAL_START_AUTHORITY_BYTES:
            raise SourceArtifactError("Formal Preview exceeds its size limit")
        _publish_once(self.root, self._path(value.preview_id), payload)
        return self.load_preview(value.preview_id)

    def load_preview(self, preview_id: UUID) -> FormalRoundPlanPreviewView:
        path = self._path(preview_id)
        if not path.exists():
            _raise_if_missing_path_is_redirected(self.root, path)
            raise NotFound("Formal Preview is not published")
        try:
            value = self._validate(FormalRoundPlanPreviewView.model_validate_json(
                _read_regular(self.root, path, MAX_FORMAL_START_AUTHORITY_BYTES),
            ))
            if value.preview_id != UUID(str(preview_id)):
                raise ValueError("Preview identity mismatch")
            return value
        except (ValidationError, ValueError, TypeError) as error:
            raise SourceArtifactError("Formal Preview snapshot is invalid") from error


AuthorityT = TypeVar(
    "AuthorityT",
    FormalExecutionStartAuthority,
    FormalEvaluationStartAuthority,
)


def _authority_path(root: Path, authority_hash: str) -> Path:
    digest = authority_hash.removeprefix("sha256:")
    if (
        not authority_hash.startswith("sha256:")
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise SourceArtifactError(
            "Formal Start Authority Store received an invalid SHA256 identity"
        )
    return root / "authorities" / "sha256" / digest[:2] / digest[2:] / "authority.json"


def _raise_if_missing_path_is_redirected(root: Path, path: Path) -> None:
    absolute_root = Path(os.path.abspath(root))
    absolute_path = Path(os.path.abspath(path))
    try:
        relative = absolute_path.relative_to(absolute_root)
    except ValueError as error:
        raise SourceArtifactError("Formal Start Authority path escaped its Store root") from error
    cursor = absolute_root
    if _is_link(cursor) or not cursor.is_dir():
        raise SourceArtifactError("Formal Start Authority Store root is not a regular directory")
    for part in relative.parts:
        cursor = cursor / part
        if _is_link(cursor):
            raise SourceArtifactError("Formal Start Authority path contains a link")
        if not cursor.exists():
            return
        if cursor != absolute_path and not cursor.is_dir():
            raise SourceArtifactError("Formal Start Authority parent is not a directory")


class DeploymentFormalStartAuthorityStore:
    """Deployment-only, content-addressed publication for signed B/D Authorities."""

    def __init__(
        self,
        root: Path,
        *,
        preview_store: DeploymentFormalStartPreviewStore,
    ) -> None:
        self.root = _prepare_no_follow_directory(root)
        self.preview_store = preview_store

    def load_preview(self, preview_id: UUID) -> FormalRoundPlanPreviewView:
        return self.preview_store.load_preview(preview_id)

    def publish_execution_authority(
        self,
        authority: FormalExecutionStartAuthority,
    ) -> FormalExecutionStartAuthority:
        value = self._validate_for_publish(
            authority,
            FormalExecutionStartAuthority,
            label="execution",
        )
        self._publish(value.authority_hash, canonical_json_bytes(value))
        return self.load_execution_authority(value.authority_hash)

    def publish_evaluation_authority(
        self,
        authority: FormalEvaluationStartAuthority,
    ) -> FormalEvaluationStartAuthority:
        value = self._validate_for_publish(
            authority,
            FormalEvaluationStartAuthority,
            label="evaluation",
        )
        self._publish(value.authority_hash, canonical_json_bytes(value))
        return self.load_evaluation_authority(value.authority_hash)

    def load_execution_authority(
        self,
        authority_hash: str,
    ) -> FormalExecutionStartAuthority:
        return self._load(
            authority_hash,
            FormalExecutionStartAuthority,
            label="execution",
        )

    def load_evaluation_authority(
        self,
        authority_hash: str,
    ) -> FormalEvaluationStartAuthority:
        return self._load(
            authority_hash,
            FormalEvaluationStartAuthority,
            label="evaluation",
        )

    def _publish(self, authority_hash: str, payload: bytes) -> None:
        if len(payload) > MAX_FORMAL_START_AUTHORITY_BYTES:
            raise SourceArtifactError("Formal Start Authority exceeds its size limit")
        path = _authority_path(self.root, authority_hash)
        try:
            _publish_once(self.root, path, payload)
        except SourceArtifactError as error:
            raise SourceArtifactError(
                "Formal Start Authority immutable identity already has other bytes"
            ) from error

    def _load(
        self,
        authority_hash: str,
        model: type[AuthorityT],
        *,
        label: str,
    ) -> AuthorityT:
        path = _authority_path(self.root, authority_hash)
        if not path.exists():
            _raise_if_missing_path_is_redirected(self.root, path)
            raise NotFound(f"Formal {label} Start Authority is not published")
        payload = _read_regular(
            self.root,
            path,
            MAX_FORMAL_START_AUTHORITY_BYTES,
        )
        try:
            authority = model.model_validate_json(payload)
        except (ValidationError, ValueError, TypeError) as error:
            raise SourceArtifactError(
                f"Formal {label} Start Authority Store entry is malformed or has another type"
            ) from error
        if authority.authority_hash != authority_hash:
            raise SourceArtifactError(
                f"Formal {label} Start Authority content Hash changed in Store"
            )
        return authority

    @staticmethod
    def _validate_for_publish(
        authority: object,
        model: type[AuthorityT],
        *,
        label: str,
    ) -> AuthorityT:
        try:
            return model.model_validate(authority)
        except (ValidationError, ValueError, TypeError) as error:
            raise SourceArtifactError(
                f"Formal {label} Start Authority is not a valid signed publication"
            ) from error


__all__ = [
    "DeploymentFormalStartAuthorityStore",
    "DeploymentFormalStartPreviewStore",
    "MAX_FORMAL_START_AUTHORITY_BYTES",
]
