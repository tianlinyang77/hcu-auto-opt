# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from hcuopt.deployment.endpoint_console import ConsoleConfig, create_app, load_config, read_summary


@pytest.fixture
def console(tmp_path):
    campaign = uuid4()
    instance = uuid4()
    key = "k" * 40
    (tmp_path / "index.html").write_text("<html>built console</html>")
    config = ConsoleConfig(campaign_id=campaign, static_root=tmp_path)
    calls = []
    payload = {"campaign": {"campaign_id": str(campaign), "automatic_release_allowed": False},
               "automatic_release_allowed": False}

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=payload)

    with httpx.Client(base_url="http://127.0.0.1:18012",
                      transport=httpx.MockTransport(respond)) as upstream:
        stopped = []
        app = create_app(config, upstream, instance, key, lambda: stopped.append(True))
        with TestClient(app, base_url="http://127.0.0.1:4196") as client:
            yield client, campaign, instance, key, calls, stopped, payload


def test_built_page_and_scoped_read(console):
    client, campaign, _, _, calls, _, _ = console
    page = client.get("/")
    assert page.status_code == 200
    assert "built console" in page.text
    assert str(campaign) in str(page.url)
    assert client.get(f"/v1/endpoint-validation-campaigns/{campaign}/summary").status_code == 200
    assert len(calls) == 1
    assert client.get(f"/v1/endpoint-validation-campaigns/{uuid4()}/summary").status_code == 403
    assert len(calls) == 1


def test_write_routes_never_forward(console):
    client, campaign, _, _, calls, _, _ = console
    assert client.post(f"/v1/endpoint-validation-campaigns/{campaign}/signoff").status_code == 403
    assert client.post("/v1/tasks").status_code == 403
    assert not calls


def test_control_requires_identity_and_key(console):
    client, _, instance, key, _, stopped, _ = console
    path = f"/v1/viewer-control/{instance}"
    assert client.post(path + "/stop").status_code == 403
    headers = {"Authorization": "Bearer " + key}
    assert client.get(path, headers=headers).json()["upstream"] == "available"
    assert client.post(f"/v1/viewer-control/{uuid4()}/stop", headers=headers).status_code == 403
    assert not stopped
    assert client.post(path + "/stop", headers=headers).json()["status"] == "stopping"
    assert stopped == [True]


def test_reject_cross_origin_and_host(console):
    client, _, _, _, _, _, _ = console
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/", headers={"Origin": "https://evil.example"}).status_code == 403


def test_invalid_authority_is_unavailable(console):
    client, campaign, _, _, _, _, payload = console
    payload["automatic_release_allowed"] = True
    response = client.get(f"/v1/endpoint-validation-campaigns/{campaign}/summary")
    assert response.status_code == 503
    assert "true" not in response.text


def test_upstream_redirect_not_followed():
    with httpx.Client(base_url="http://127.0.0.1:18012", follow_redirects=False,
                      transport=httpx.MockTransport(lambda _: httpx.Response(
                          302, headers={"Location": "https://example.org"}))) as client:
        with pytest.raises(httpx.HTTPStatusError):
            read_summary(client, uuid4())


def test_config_resolves_build_and_rejects_ssh_option(tmp_path):
    (tmp_path / "index.html").write_text("built")
    path = tmp_path / "console.json"
    config = {"campaign_id": str(uuid4()), "static_root": ".", "ssh_target": "github@host"}
    path.write_text(json.dumps(config))
    assert load_config(path).static_root == tmp_path
    config["ssh_target"] = "-bad-option"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        load_config(path)
