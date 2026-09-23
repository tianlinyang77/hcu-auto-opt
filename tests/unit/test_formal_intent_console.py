# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from hcuopt.api.formal_start_management import FormalIntentSubmission
from hcuopt.deployment.formal_intent_console import create_formal_intent_console
from tests.unit.test_formal_start_management_api import TOKEN, setup_management


def console(tmp_path, origin="http://127.0.0.1:4198"):  # type: ignore[no-untyped-def]
    management, repository, payload = setup_management(tmp_path)
    management = replace(
        management,
        capabilities=(
            replace(
                management.capabilities[0],
                submission=FormalIntentSubmission.model_validate(payload),
            ),
        ),
    )
    root = tmp_path / "web"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<h1>Formal intent</h1>", encoding="utf-8")
    app = create_formal_intent_console(
        management=management, repository=repository, static_root=root, browser_origin=origin
    )
    return app, repository, payload


def test_console_only_exposes_preparation_and_submission(tmp_path):  # type: ignore[no-untyped-def]
    app, repo, payload = console(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:4198") as client:
        assert client.get("/").status_code == 200
        headers = {"Authorization": f"Bearer {TOKEN}"}
        assert client.get("/v1/operator/formal-start-submission", headers=headers).json() == payload
        result = client.post("/v1/operator/formal-start-intents", headers=headers, json=payload)
        assert result.status_code == 200
        assert result.json()["hcu_accessed"] is False
        for path in (
            "/v1/tasks",
            "/v1/jobs",
            "/v1/search-rounds",
            "/v1/leases",
            "/docs",
            "/openapi.json",
            "/v1/operator/formal-round-dispatch",
        ):
            for method in ("GET", "POST", "DELETE"):
                blocked = client.request(method, path, headers=headers)
                assert blocked.status_code == 404
                assert blocked.headers["cache-control"] == "no-store"
    assert len(repo.intents) == 1


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.example"},
        {"Origin": "https://evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_console_rejects_cross_origin_before_write(tmp_path, headers):  # type: ignore[no-untyped-def]
    app, repo, payload = console(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:4198") as client:
        response = client.post(
            "/v1/operator/formal-start-intents",
            json=payload,
            headers={"Authorization": f"Bearer {TOKEN}", **headers},
        )
    assert response.status_code == 403
    assert not repo.intents


@pytest.mark.parametrize(
    "origin", ["http://example.com", "https://user:pass@example.com", "https://example.com/path"]
)
def test_console_rejects_unsafe_origin_config(tmp_path, origin):  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="origin"):
        console(tmp_path, origin)
