# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from hcuopt.domain.errors import Conflict, NotFound, SourceArtifactError
from hcuopt.evaluation.m2_formal_start_authority import M2FormalEvaluationStartRegistration
from hcuopt.measurement.evidence import canonical_json_bytes


def evaluation_registration_hash(value: M2FormalEvaluationStartRegistration) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _decode(row: dict) -> M2FormalEvaluationStartRegistration:
    try:
        value = M2FormalEvaluationStartRegistration.model_validate(row["registration"])
        expected = {
            "registration_id": value.registration_id,
            "preview_id": value.preview_id,
            "formal_authorization_hash": value.formal_authorization_hash,
            "resolved_plan_hash": value.resolved_plan_hash,
            "content_hash": evaluation_registration_hash(value),
        }
        if any(row.get(key) != item for key, item in expected.items()):
            raise ValueError("registration identity drift")
    except (KeyError, ValidationError, ValueError, TypeError) as error:
        raise SourceArtifactError("Formal evaluation registration content changed") from error
    return value


class DeploymentFormalEvaluationStartRegistry:
    """D-owned immutable registrations; deployment credentials own the connection factory.

    Pass PostgresRepository.connection after running the normal migrations. This
    adapter has no signer, HCU access, or authority to authorize a time window.
    """

    def __init__(
        self,
        connection_factory: Callable[[], AbstractContextManager[Connection[dict[str, Any]]]],
    ) -> None:
        self.connection_factory = connection_factory

    def publish_registration(
        self, registration: M2FormalEvaluationStartRegistration
    ) -> M2FormalEvaluationStartRegistration:
        value = M2FormalEvaluationStartRegistration.model_validate(
            registration.model_dump(mode="json")
        )
        content_hash = evaluation_registration_hash(value)
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                INSERT INTO formal_evaluation_start_registrations (
                    registration_id, preview_id, formal_authorization_hash,
                    resolved_plan_hash, content_hash, registration
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING RETURNING *
                """,
                (
                    value.registration_id, value.preview_id,
                    value.formal_authorization_hash, value.resolved_plan_hash,
                    content_hash, Jsonb(value.model_dump(mode="json")),
                ),
            ).fetchone()
            if row is None:
                rows = connection.execute(
                    """
                    SELECT * FROM formal_evaluation_start_registrations
                    WHERE registration_id = %s OR preview_id = %s OR content_hash = %s
                    """,
                    (value.registration_id, value.preview_id, content_hash),
                ).fetchall()
                if len(rows) != 1:
                    raise Conflict("Formal evaluation registration identities conflict")
                row = rows[0]
            stored = _decode(row)
            if canonical_json_bytes(stored) != canonical_json_bytes(value):
                raise Conflict("Formal evaluation registration cannot be rebound")
        return stored

    def load_registration(
        self, *, preview_id: UUID, formal_authorization_hash: str, resolved_plan_hash: str
    ) -> M2FormalEvaluationStartRegistration:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT * FROM formal_evaluation_start_registrations
                WHERE preview_id = %s
                """,
                (preview_id,),
            ).fetchone()
        if row is None:
            raise NotFound("Formal evaluation registration is not published")
        value = _decode(row)
        if (
            value.preview_id != preview_id
            or value.formal_authorization_hash != formal_authorization_hash
            or value.resolved_plan_hash != resolved_plan_hash
        ):
            raise SourceArtifactError(
                "Formal evaluation registration belongs to another plan/window"
            )
        return value
