# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Real loopback HTTP startup with isolated PostgreSQL and synthetic authority."""

import json
import os
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from hcuopt.deployment.formal_service import build_formal_service
from hcuopt.storage.repository import PostgresRepository
from tests.integration.test_formal_start_management_postgres import isolated_dsn  # noqa: F401
from tests.unit.test_formal_service import service_fixture

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.getenv("HCUOPT_DATABASE_URL"), reason="requires PostgreSQL"),
]


def test_configuration_starts_real_http_without_jobs(tmp_path, request):
    dsn = request.getfixturevalue("isolated_dsn")
    raw, kwargs = service_fixture(tmp_path)
    kwargs["database_url"] = dsn
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        raw["port"] = listener.getsockname()[1]
        kwargs["configuration_path"].write_text(json.dumps(raw), encoding="utf-8")
        app, port = build_formal_service(**kwargs)
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="critical"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                assert thread.is_alive() and time.monotonic() < deadline
                time.sleep(0.02)
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5,
            ) as client:
                assert client.get("/").status_code == 200
                response = client.get("/", headers={"origin": "https://evil.example"})
                assert response.status_code == 403
            with PostgresRepository(dsn).connection() as conn:
                for table in ("jobs", "formal_round_dispatches", "formal_dispatch_claims"):
                    assert conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
