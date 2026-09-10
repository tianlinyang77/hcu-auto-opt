# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict

from hcuopt.cli import main
from hcuopt.deployment import viewer_service as service
from hcuopt.deployment.framework_smoke_viewer import create_viewer
from tests.unit.test_framework_smoke_viewer import projection_fixture

DSN = "host=127.0.0.1 port=55433 user=fixture password=private-fixture dbname=fixture"


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def config(tmp_path, port=None):
    static = tmp_path / "static"
    static.mkdir(exist_ok=True)
    (static / "index.html").write_text("<h1>Fixture</h1>", encoding="utf-8")
    return service.ViewerConfig(
        task_id=uuid4(), database_schema="test_schema", static_root=static, port=port or free_port()
    )


def test_readonly_dsn_overrides_and_loopback():
    fields = conninfo_to_dict(service.readonly_dsn(DSN, "test_schema", 12345))
    assert fields["host"] == fields["hostaddr"] == "127.0.0.1"
    assert fields["port"] == "12345"
    assert "default_transaction_read_only=on" in fields["options"]
    assert "search_path=test_schema" in fields["options"]
    assert "public" not in fields["options"]


@pytest.mark.parametrize(
    "dsn",
    [
        "",
        DSN.replace("127.0.0.1", "example.com"),
        DSN + " options='-c default_transaction_read_only=off'",
        DSN + " service=evil",
        DSN + " hostaddr=10.1.1.1",
        DSN.replace("55433", "0"),
    ],
)
def test_unsafe_dsn_rejected_without_leaking_password(dsn):
    with pytest.raises(service.ViewerServiceError) as error:
        service.readonly_dsn(dsn, "test_schema")
    assert "private-fixture" not in str(error.value)


def test_config_rejects_unknown_credentials_and_invalid_schema(tmp_path):
    cfg = config(tmp_path).model_dump(mode="json")
    cfg["database_password"] = "secret"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(service.ViewerServiceError, match="invalid_viewer_config"):
        service.load_config(path)
    with pytest.raises(service.ViewerServiceError):
        service.readonly_dsn(DSN, "test_schema,public")


def test_control_key_is_not_read_key_and_has_no_signoff_authority():
    task, instance = uuid4(), uuid4()
    stopped = threading.Event()
    app = create_viewer(
        task_id=task,
        credential="r" * 40,
        read_summary=lambda: None,
        control=(instance, "c" * 40, stopped.set),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        url = f"/v1/viewer-control/{instance}"
        assert client.post(url + "/stop").status_code == 401
        assert (
            client.post(url + "/stop", headers={"Authorization": "Bearer " + "r" * 40}).status_code
            == 401
        )
        control = {"Authorization": "Bearer " + "c" * 40}
        assert client.post(f"/v1/viewer-control/{uuid4()}/stop", headers=control).status_code == 403
        assert not stopped.is_set()
        assert client.post(url + "/stop", headers=control).status_code == 200
        assert stopped.is_set()
        assert (
            client.post(f"/v1/framework-smoke/tasks/{task}/signoff", headers=control).status_code
            == 404
        )


def wait_record(parent, wanted="running"):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        for path in parent.glob("*/instance.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["status"] == wanted:
                return path.parent, record
        time.sleep(0.05)
    raise AssertionError("service did not reach " + wanted)


def test_live_lifecycle_read_status_stop_revoke(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    summary = projection_fixture()
    summary.task.task_id = cfg.task_id
    summary.evidence_bundles[0]["task_id"] = cfg.task_id
    monkeypatch.setattr(service, "make_reader", lambda *_: lambda: summary)
    parent = tmp_path / "instances"
    errors = []

    def run():
        try:
            service.serve(cfg, DSN, instance_parent=parent)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    directory = None
    try:
        directory, record = wait_record(parent)
        access = (directory / "access-key.txt").read_text()
        control = (directory / "control-key.txt").read_text()
        assert access != control
        with httpx.Client(trust_env=False, timeout=3) as client:
            url = f"http://127.0.0.1:{cfg.port}/v1/framework-smoke-inspection/{cfg.task_id}"
            assert client.get(url).status_code == 401
            data = client.get(url, headers={"Authorization": "Bearer " + access}).json()
            assert data["ready_for_human_review"] is True
            assert data["write_actions_available"] is False
        assert service.control_instance(directory, "status")["status"] == "running"
        assert service.control_instance(directory, "stop")["status"] == "stopping"
        thread.join(timeout=10)
        assert not thread.is_alive() and errors == []
        assert not (directory / "access-key.txt").exists()
        assert not (directory / "control-key.txt").exists()
        assert service.control_instance(directory, "status")["status"] == "stopped"
    finally:
        if thread.is_alive() and directory:
            service.control_instance(directory, "stop")
            thread.join(timeout=10)


def test_database_failure_revokes_credentials(tmp_path, monkeypatch):
    cfg = config(tmp_path)

    def fail():
        raise RuntimeError("password=private-fixture")

    monkeypatch.setattr(service, "make_reader", lambda *_: fail)
    parent = tmp_path / "instances"
    with pytest.raises(service.ViewerServiceError) as error:
        service.serve(cfg, DSN, instance_parent=parent)
    assert "private-fixture" not in str(error.value)
    directory, record = wait_record(parent, "failed")
    assert not list(directory.glob("*-key.txt"))


def test_owned_tunnel_cleanup_on_failure(tmp_path, monkeypatch):
    cfg = config(tmp_path).model_copy(update={"ssh_target": "fixture@127.0.0.1"})
    calls = []

    class Process:
        def poll(self):
            return None

        def terminate(self):
            calls.append("terminate")

        def wait(self, timeout):
            calls.append("wait")

    def launch(argv, **kwargs):
        assert argv[0] == "ssh" and "-N" in argv
        assert "StrictHostKeyChecking=yes" in argv
        assert "private-fixture" not in str(argv)
        calls.append("launch")
        return Process()

    monkeypatch.setattr(service.subprocess, "Popen", launch)
    with (
        pytest.raises(RuntimeError),
        service.database_endpoint(cfg, before_close=lambda: calls.append("revoke")),
    ):
        raise RuntimeError("fixture")
    assert calls == ["launch", "revoke", "terminate", "wait"]


def test_interrupt_with_live_thread_cannot_return_success(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    monkeypatch.setattr(service, "make_reader", lambda *_: lambda: projection_fixture())
    servers = []

    class Server:
        started = True
        should_exit = False

        def __init__(self, *_):
            servers.append(self)

    class Thread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

        def join(self, timeout):
            assert servers[0].should_exit

        def is_alive(self):
            return True

    def interrupt(**kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(service.uvicorn, "Server", Server)
    monkeypatch.setattr(service, "threading", SimpleNamespace(Thread=Thread, Event=threading.Event))
    monkeypatch.setattr(service.httpx, "Client", interrupt)
    parent = tmp_path / "instances"
    with pytest.raises(service.ViewerServiceError, match="viewer_shutdown_incomplete"):
        service.serve(cfg, DSN, instance_parent=parent)
    directory, _ = wait_record(parent, "failed")
    assert not list(directory.glob("*-key.txt"))


def test_credential_cleanup_attempts_both_files(tmp_path, monkeypatch):
    cfg = config(tmp_path)

    def interrupt():
        raise KeyboardInterrupt

    monkeypatch.setattr(service, "make_reader", lambda *_: interrupt)
    original_unlink = Path.unlink
    attempted = []

    def unlink(path, *args, **kwargs):
        attempted.append(path.name)
        if path.name == "access-key.txt":
            raise PermissionError("private-fixture")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    parent = tmp_path / "instances"
    with pytest.raises(service.ViewerServiceError, match="viewer_cleanup_incomplete") as error:
        service.serve(cfg, DSN, instance_parent=parent)
    assert "private-fixture" not in str(error.value)
    directory, _ = wait_record(parent, "failed")
    assert attempted == ["access-key.txt", "control-key.txt"]
    assert not (directory / "control-key.txt").exists()
    original_unlink(directory / "access-key.txt")


def test_cli_missing_dsn_is_safe(tmp_path, monkeypatch, capsys):
    cfg = config(tmp_path)
    path = tmp_path / "config.json"
    path.write_text(cfg.model_dump_json(), encoding="utf-8")
    monkeypatch.delenv("HCUOPT_VIEWER_DATABASE_URL", raising=False)
    assert main(["framework-viewer", "serve", str(path)]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_loopback_database_config"


def test_busy_port_does_not_stop_existing_listener(tmp_path, monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        cfg = config(tmp_path, listener.getsockname()[1])
        summary = projection_fixture()
        summary.task.task_id = cfg.task_id
        summary.evidence_bundles[0]["task_id"] = cfg.task_id
        monkeypatch.setattr(service, "make_reader", lambda *_: lambda: summary)
        parent = tmp_path / "instances"
        with pytest.raises(service.ViewerServiceError):
            service.serve(cfg, DSN, instance_parent=parent)
        directory, _ = wait_record(parent, "failed")
        assert not list(directory.glob("*-key.txt"))
        with socket.create_connection(listener.getsockname(), timeout=1):
            accepted, _ = listener.accept()
            accepted.close()


def test_stop_refuses_wrong_live_identity(tmp_path, monkeypatch):
    instance = uuid4()
    directory = tmp_path / str(instance)
    directory.mkdir()
    service.write_record(
        directory / "instance.json",
        {"instance_id": str(instance), "task_id": str(uuid4()), "port": 4194, "status": "running"},
    )
    (directory / "control-key.txt").write_text("x" * 40)

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            return httpx.Response(
                200,
                request=httpx.Request("GET", "http://127.0.0.1"),
                json={"instance_id": str(uuid4())},
            )

        def post(self, *args, **kwargs):
            raise AssertionError("must not stop another instance")

    monkeypatch.setattr(service.httpx, "Client", Client)
    with pytest.raises(service.ViewerServiceError, match="control_unavailable"):
        service.control_instance(directory, "stop")
