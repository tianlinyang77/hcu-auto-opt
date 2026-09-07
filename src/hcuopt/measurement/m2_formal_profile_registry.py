# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from pathlib import Path

from pydantic import ValidationError

from hcuopt.domain.errors import NotFound, SourceArtifactError
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.measurement.m2_formal_receipt import (
    _is_link,
    _prepare_no_follow_directory,
    _publish_once,
    _read_regular,
)
from hcuopt.measurement.m2_formal_start_authority import (
    M2FormalExecutionProfileRegistration,
)

MAX_FORMAL_EXECUTION_PROFILE_REGISTRATION_BYTES = 512 * 1024
MAX_FORMAL_EXECUTION_PROFILE_BINDING_BYTES = 4096
_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,199}$")


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _digest(value: str, *, label: str) -> str:
    digest = value.removeprefix("sha256:")
    if (
        not value.startswith("sha256:")
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise SourceArtifactError(f"Formal execution Profile Registry received invalid {label}")
    return digest


def m2_formal_execution_profile_registration_hash(
    registration: M2FormalExecutionProfileRegistration,
) -> str:
    value = M2FormalExecutionProfileRegistration.model_validate(
        registration.model_dump(mode="json")
    )
    return _sha256(canonical_json_bytes(value))


def _registration_path(root: Path, content_hash: str) -> Path:
    digest = _digest(content_hash, label="registration Hash")
    return root / "registrations" / "sha256" / digest[:2] / digest[2:] / "registration.json"


def _key_binding_path(root: Path, profile_id: str, authorization_hash: str) -> Path:
    if _PROFILE_ID.fullmatch(profile_id) is None:
        raise SourceArtifactError("Formal execution Profile Registry received invalid Profile ID")
    digest = _digest(authorization_hash, label="authorization Hash")
    return root / "bindings" / "authorization" / digest / f"{profile_id}.json"


def _id_binding_path(root: Path, registration_id: object) -> Path:
    return root / "bindings" / "registration-id" / f"{registration_id}.json"


def _raise_if_missing_path_is_redirected(root: Path, path: Path) -> None:
    absolute_root = Path(os.path.abspath(root))
    absolute_path = Path(os.path.abspath(path))
    try:
        relative = absolute_path.relative_to(absolute_root)
    except ValueError as error:
        raise SourceArtifactError(
            "Formal execution Profile Registry path escaped its root"
        ) from error
    cursor = absolute_root
    if _is_link(cursor) or not cursor.is_dir():
        raise SourceArtifactError(
            "Formal execution Profile Registry root is not a regular directory"
        )
    for part in relative.parts:
        cursor = cursor / part
        if _is_link(cursor):
            raise SourceArtifactError(
                "Formal execution Profile Registry path contains a link"
            )
        if not cursor.exists():
            return
        if cursor != absolute_path and not cursor.is_dir():
            raise SourceArtifactError(
                "Formal execution Profile Registry parent is not a directory"
            )


class DeploymentM2FormalExecutionProfileRegistry:
    """Deployment-owned, write-once B Profile registration boundary."""

    def __init__(self, root: Path) -> None:
        self.root = _prepare_no_follow_directory(root)

    def publish_registration(
        self,
        registration: M2FormalExecutionProfileRegistration,
    ) -> M2FormalExecutionProfileRegistration:
        try:
            value = M2FormalExecutionProfileRegistration.model_validate(
                registration.model_dump(mode="json")
            )
        except (AttributeError, ValidationError, ValueError, TypeError) as error:
            raise SourceArtifactError(
                "Formal execution Profile registration is malformed"
            ) from error
        payload = canonical_json_bytes(value)
        if len(payload) > MAX_FORMAL_EXECUTION_PROFILE_REGISTRATION_BYTES:
            raise SourceArtifactError(
                "Formal execution Profile registration exceeds its size limit"
            )
        content_hash = _sha256(payload)
        binding = canonical_json_bytes(
            {
                "schema_version": "m2a-formal-execution-profile-registration-binding-v1",
                "registration_id": str(value.registration_id),
                "adapter_profile_id": value.profile.profile_id,
                "formal_authorization_hash": value.formal_authorization_hash,
                "content_hash": content_hash,
            }
        )
        if len(binding) > MAX_FORMAL_EXECUTION_PROFILE_BINDING_BYTES:
            raise SourceArtifactError(
                "Formal execution Profile registration binding exceeds its size limit"
            )
        try:
            _publish_once(self.root, _registration_path(self.root, content_hash), payload)
            _publish_once(
                self.root,
                _id_binding_path(self.root, value.registration_id),
                binding,
            )
            _publish_once(
                self.root,
                _key_binding_path(
                    self.root,
                    value.profile.profile_id,
                    value.formal_authorization_hash,
                ),
                binding,
            )
        except SourceArtifactError as error:
            raise SourceArtifactError(
                "Formal execution Profile registration identity already has other bytes"
            ) from error
        return self.load_registration(
            adapter_profile_id=value.profile.profile_id,
            formal_authorization_hash=value.formal_authorization_hash,
        )

    def load_registration(
        self,
        *,
        adapter_profile_id: str,
        formal_authorization_hash: str,
    ) -> M2FormalExecutionProfileRegistration:
        binding_path = _key_binding_path(
            self.root,
            adapter_profile_id,
            formal_authorization_hash,
        )
        binding_payload = self._read_binding(binding_path)
        try:
            binding = json.loads(binding_payload)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
            raise SourceArtifactError(
                "Formal execution Profile registration binding is malformed"
            ) from error
        required_keys = {
            "schema_version",
            "registration_id",
            "adapter_profile_id",
            "formal_authorization_hash",
            "content_hash",
        }
        if (
            not isinstance(binding, dict)
            or set(binding) != required_keys
            or binding.get("schema_version")
            != "m2a-formal-execution-profile-registration-binding-v1"
            or binding.get("adapter_profile_id") != adapter_profile_id
            or binding.get("formal_authorization_hash") != formal_authorization_hash
        ):
            raise SourceArtifactError(
                "Formal execution Profile registration binding changed"
            )
        content_hash = binding.get("content_hash")
        if not isinstance(content_hash, str):
            raise SourceArtifactError(
                "Formal execution Profile registration binding changed"
            )
        path = _registration_path(self.root, content_hash)
        if not path.exists():
            _raise_if_missing_path_is_redirected(self.root, path)
            raise NotFound("Formal execution Profile registration content is not published")
        payload = _read_regular(
            self.root,
            path,
            MAX_FORMAL_EXECUTION_PROFILE_REGISTRATION_BYTES,
        )
        try:
            registration = M2FormalExecutionProfileRegistration.model_validate_json(payload)
        except (ValidationError, ValueError, TypeError) as error:
            raise SourceArtifactError(
                "Formal execution Profile registration content is malformed"
            ) from error
        expected_binding = canonical_json_bytes(
            {
                "schema_version": "m2a-formal-execution-profile-registration-binding-v1",
                "registration_id": str(registration.registration_id),
                "adapter_profile_id": registration.profile.profile_id,
                "formal_authorization_hash": registration.formal_authorization_hash,
                "content_hash": _sha256(payload),
            }
        )
        if (
            not hmac.compare_digest(binding_payload, expected_binding)
            or _sha256(payload) != content_hash
            or registration.profile.profile_id != adapter_profile_id
            or registration.formal_authorization_hash != formal_authorization_hash
        ):
            raise SourceArtifactError(
                "Formal execution Profile registration content differs from its binding"
            )
        id_binding = self._read_binding(
            _id_binding_path(self.root, registration.registration_id)
        )
        if not hmac.compare_digest(id_binding, expected_binding):
            raise SourceArtifactError(
                "Formal execution Profile registration ID was rebound"
            )
        return registration

    def _read_binding(self, path: Path) -> bytes:
        if not path.exists():
            _raise_if_missing_path_is_redirected(self.root, path)
            raise NotFound("Formal execution Profile registration is not published")
        return _read_regular(
            self.root,
            path,
            MAX_FORMAL_EXECUTION_PROFILE_BINDING_BYTES,
        )


__all__ = [
    "DeploymentM2FormalExecutionProfileRegistry",
    "MAX_FORMAL_EXECUTION_PROFILE_BINDING_BYTES",
    "MAX_FORMAL_EXECUTION_PROFILE_REGISTRATION_BYTES",
    "m2_formal_execution_profile_registration_hash",
]
