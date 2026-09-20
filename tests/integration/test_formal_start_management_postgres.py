# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Real Intent persistence; plan authorities/signatures are explicit test fixtures."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from hcuopt.api.app import create_app
from hcuopt.api.formal_start_management import FormalStartManagement
from hcuopt.storage.repository import PostgresRepository
from tests.unit.test_formal_start_management_api import TOKEN, setup_management

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.getenv("HCUOPT_DATABASE_URL"), reason="requires PostgreSQL"),
]


@pytest.fixture
def isolated_dsn():  # type: ignore[no-untyped-def]
    base = os.environ["HCUOPT_DATABASE_URL"]
    schema = "formal_http_" + uuid4().hex
    with psycopg.connect(base) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        dsn = make_conninfo(base, options=f"-c search_path={schema}")
        with psycopg.connect(dsn) as connection:
            assert connection.execute("SELECT current_schema()").fetchone()[0] == schema
        PostgresRepository(dsn).migrate()
        yield dsn
    finally:
        assert schema.startswith("formal_http_") and len(schema) == 44
        with psycopg.connect(base) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_http_concurrent_retry_restart_and_revocation(isolated_dsn, tmp_path):  # type: ignore[no-untyped-def]
    management, memory, payload = setup_management(tmp_path)

    class FixtureAuthorityRepository(PostgresRepository):
        # Only the pre-existing authority snapshot is synthetic. All Intent
        # transactions, uniqueness constraints and audit events use PostgreSQL.
        def resolve_formal_operator_authority(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return memory.resolve_formal_operator_authority(*args, **kwargs)

        def assert_operator_candidate_ids_available(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return memory.assert_operator_candidate_ids_available(*args, **kwargs)

    config = tmp_path / "capabilities.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": "formal-intent-capabilities-v1",
                "capabilities": [
                    {
                        "token_sha256": sha256(TOKEN.encode()).hexdigest(),
                        "assertion": management.capabilities[0].assertion.model_dump(mode="json"),
                        "submission": payload,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def make_app():  # type: ignore[no-untyped-def]
        loaded = FormalStartManagement.from_file(
            management.coordinator, deployment_root=tmp_path, path=config
        )
        return create_app(
            repository=FixtureAuthorityRepository(isolated_dsn),
            formal_start_management=loaded,
            auto_migrate=False,
        )

    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(make_app()) as client:
        prepared = client.get("/v1/operator/formal-start-submission", headers=headers)
        assert prepared.status_code == 200
        assert prepared.json() == payload

        def submit(_):  # type: ignore[no-untyped-def]
            return client.post("/v1/operator/formal-start-intents", json=payload, headers=headers)

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(submit, range(2)))
    assert all(response.status_code == 200 for response in responses), [r.text for r in responses]
    assert {r.json()["replayed"] for r in responses} == {False, True}
    intent_id = UUID(responses[0].json()["intent_id"])

    # Treat previous response as lost; recreate app/repository/config loader.
    with TestClient(make_app()) as restarted:
        replay = restarted.post("/v1/operator/formal-start-intents", json=payload, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert UUID(replay.json()["intent_id"]) == intent_id
    for name in ("round_creation_allowed", "hcu_accessed", "automatic_release_allowed"):
        assert replay.json()[name] is False

    config.write_text(
        '{"schema_version":"formal-intent-capabilities-v1","capabilities":[]}', encoding="utf-8"
    )
    with TestClient(make_app()) as revoked:
        denied = revoked.post("/v1/operator/formal-start-intents", json=payload, headers=headers)
    assert denied.status_code == 403
    with psycopg.connect(isolated_dsn) as connection:
        assert (
            connection.execute("SELECT count(*) FROM formal_operator_start_intents").fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM formal_operator_start_intent_events"
            ).fetchone()[0]
            == 4
        )
        for table in ("search_rounds", "tasks", "jobs"):
            assert (
                connection.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                ).fetchone()[0]
                == 0
            )
