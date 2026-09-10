# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from hcuopt.deployment.framework_smoke_viewer import create_viewer, project_summary
from hcuopt.domain.enums import TaskState


def test_auth_and_task_boundary_do_not_read_database():
    def forbidden():
        raise AssertionError("unauthorized requests must not reach the read model")

    identifier = uuid4()
    app = create_viewer(task_id=identifier, credential="a" * 40, read_summary=forbidden)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        path = f"/v1/framework-smoke-inspection/{identifier}"
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert (
            client.get(
                f"/v1/framework-smoke-inspection/{uuid4()}",
                headers={"Authorization": "Bearer " + "a" * 40},
            ).status_code
            == 403
        )
        assert client.post(path).status_code == 405
        assert client.get(path, headers={"Host": "evil.example"}).status_code == 403


def test_backend_errors_redacted_and_no_cache():
    def unavailable():
        raise RuntimeError("postgresql://secret-password@host")

    identifier = uuid4()
    app = create_viewer(task_id=identifier, credential="a" * 40, read_summary=unavailable)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get(
            f"/v1/framework-smoke-inspection/{identifier}",
            headers={"Authorization": "Bearer " + "a" * 40},
        )
    assert response.status_code == 503
    assert "secret" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


def test_wrong_database_task_rejected():
    identifier = uuid4()
    app = create_viewer(
        task_id=identifier,
        credential="a" * 40,
        read_summary=lambda: SimpleNamespace(task=SimpleNamespace(task_id=uuid4())),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/openapi.json").status_code == 404
        assert (
            client.get(
                f"/v1/framework-smoke-inspection/{identifier}",
                headers={"Authorization": "Bearer " + "a" * 40},
            ).status_code
            == 503
        )


@pytest.mark.parametrize("credential", ["short", "中文" * 32])
def test_invalid_credential_not_accepted(credential):
    with pytest.raises(ValueError):
        create_viewer(task_id=uuid4(), credential=credential, read_summary=lambda: None)


def projection_fixture():
    now = datetime.now(timezone.utc)
    task_id, evaluation_id = uuid4(), uuid4()
    metrics = {
        key: True
        for key in (
            "baseline_execution_succeeded",
            "noop_execution_succeeded",
            "output_equivalent",
            "cleanup_healthy",
        )
    }
    return SimpleNamespace(
        task=SimpleNamespace(
            task_id=task_id,
            name="fixture",
            state=TaskState.AWAITING_SIGNOFF,
            adapter_profile="fixture",
            updated_at=now,
        ),
        target=SimpleNamespace(
            target_id="fixture",
            source_baseline=SimpleNamespace(commit="a" * 40),
            inference_image=SimpleNamespace(registry_digest="sha256:" + "a" * 64),
            execution_host=SimpleNamespace(
                name="fixture", accelerator=SimpleNamespace(device_index=7, architecture="gfx936")
            ),
        ),
        evaluations=[
            {
                "created_at": now,
                "evaluation_run_id": evaluation_id,
                "passed": True,
                "synthetic": False,
                "metrics": metrics,
            }
        ],
        evidence_bundles=[
            {
                "evidence_id": uuid4(),
                "task_id": task_id,
                "evaluation_run_id": evaluation_id,
                "created_at": now,
            }
        ],
        source_snapshots=[],
        artifacts=[],
        execution_attempts=[],
        events=[],
        adapter_mode="real",
    )


def test_failed_cleanup_never_upgraded_from_output_equivalence():
    summary = projection_fixture()
    summary.task.state = TaskState.REJECTED
    summary.evaluations[0]["metrics"]["cleanup_healthy"] = False
    result = project_summary(summary)
    assert result["checks"]["output_equivalent"] is True
    assert result["checks"]["cleanup_healthy"] is False
    assert not result["ready_for_human_review"]
    assert result["performance_conclusion"] == "not_measured"
    assert not result["automatic_release_allowed"] and not result["write_actions_available"]


@pytest.mark.parametrize("case", ["missing_evidence", "synthetic", "string_boolean"])
def test_incomplete_or_synthetic_never_ready(case):
    summary = projection_fixture()
    if case == "missing_evidence":
        summary.evidence_bundles = []
    elif case == "synthetic":
        summary.evaluations[0]["synthetic"] = True
    else:
        summary.evaluations[0]["metrics"]["cleanup_healthy"] = "true"
    assert not project_summary(summary)["ready_for_human_review"]


def test_projection_does_not_expose_event_payloads():
    summary = projection_fixture()
    summary.events = [
        {"event_type": "test", "created_at": "2026-09-09", "details": {"secret": "must-not-appear"}}
    ]
    result = project_summary(summary)
    assert "must-not-appear" not in str(result)
    assert result["ready_for_human_review"]


@pytest.mark.parametrize("field", ["task_id", "evaluation_run_id"])
def test_unrelated_evidence_cannot_make_review_ready(field):
    summary = projection_fixture()
    summary.evidence_bundles[0][field] = uuid4()
    result = project_summary(summary)
    assert result["evaluation_passed"] is True
    assert result["ready_for_human_review"] is False
    assert result["review_evidence_id"] is None


def test_new_unbundled_evaluation_cannot_reuse_old_evidence():
    from datetime import timedelta

    summary = projection_fixture()
    newer = dict(
        summary.evaluations[0],
        evaluation_run_id=uuid4(),
        created_at=summary.evaluations[0]["created_at"] + timedelta(seconds=1),
    )
    summary.evaluations.append(newer)
    assert project_summary(summary)["ready_for_human_review"] is False


def test_latest_bundle_order_matches_repository():
    from datetime import timedelta

    summary = projection_fixture()
    summary.evidence_bundles.append(
        dict(
            summary.evidence_bundles[0],
            evidence_id=uuid4(),
            evaluation_run_id=uuid4(),
            created_at=summary.evidence_bundles[0]["created_at"] + timedelta(seconds=1),
        )
    )
    assert project_summary(summary)["ready_for_human_review"] is False


def test_stopping_revokes_access_before_database_read():
    identifier = uuid4()
    app = create_viewer(
        task_id=identifier,
        credential="a" * 40,
        access_active=lambda: False,
        read_summary=lambda: pytest.fail("must not read during shutdown"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get(
            f"/v1/framework-smoke-inspection/{identifier}",
            headers={"Authorization": "Bearer " + "a" * 40},
        )
        assert response.status_code == 503
