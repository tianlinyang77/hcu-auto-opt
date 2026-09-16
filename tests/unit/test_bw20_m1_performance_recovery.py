# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from hcuopt.deployment.bw20_m1_performance_recovery import (
    CANDIDATE_ID,
    PERFORMANCE_JOB_ID,
    RESOURCE_ID,
    TASK_ID,
    _validate_mirror_snapshot,
    _validate_snapshot,
)


def _snapshot():
    task = {
        "task_id": TASK_ID,
        "workflow_type": "manual_candidate",
        "state": "rejected",
        "automatic_release_allowed": False,
    }
    candidate = {
        "candidate_id": CANDIDATE_ID,
        "task_id": TASK_ID,
        "state": "rejected",
        "verdict": None,
        "evidence_bundle_id": None,
    }
    job = {
        "job_id": PERFORMANCE_JOB_ID,
        "task_id": TASK_ID,
        "job_type": "manual_performance",
        "state": "failed",
        "attempts": 3,
        "max_attempts": 3,
        "result": None,
        "last_error": {"code": "RuntimeError", "message": "host namespace unavailable"},
    }
    jobs = [
        {"job_type": "manual_build", "state": "succeeded"},
        {"job_type": "manual_correctness", "state": "succeeded"},
        {"job_type": "manual_performance", "state": "failed"},
    ]
    resource = {
        "resource_id": RESOURCE_ID,
        "state": "available",
        "owner_job_id": None,
        "lease_id": None,
        "fencing_token": 46,
        "cleanup_evidence": {
            "healthy": True,
            "fence": {"fenced": True},
            "health": {"healthy": True},
        },
    }
    return task, candidate, job, jobs, resource


def test_exact_cleaned_namespace_failure_is_recoverable() -> None:
    task, candidate, job, jobs, resource = _snapshot()
    _validate_snapshot(
        task=task,
        candidate=candidate,
        job=job,
        jobs=jobs,
        resource=resource,
        signoff_exists=False,
        evaluation_count=0,
    )


def test_recovery_rejects_any_drift() -> None:
    task, candidate, job, jobs, resource = _snapshot()
    job["last_error"] = {"code": "RuntimeError", "message": "different failure"}
    try:
        _validate_snapshot(
            task=task,
            candidate=candidate,
            job=job,
            jobs=jobs,
            resource=resource,
            signoff_exists=False,
            evaluation_count=0,
        )
    except RuntimeError as exc:
        assert "exact cleaned namespace" in str(exc)
    else:
        raise AssertionError("drifted recovery snapshot must be rejected")


def test_exact_cleaned_local_mirror_failure_is_recoverable() -> None:
    task, candidate, job, jobs, resource = _snapshot()
    job.update(
        attempts=4,
        max_attempts=4,
        last_error={
            "code": "CalledProcessError",
            "message": (
                "Command ['scp', 'github@10.17.1.20:/home/github/hcu-auto-opt-runtime/"
                "bw20-m1/run/evidence/cache-namespace.json', '/home/github/"
                "hcu-auto-opt-runtime/bw20-m1-worker-raw/run/cache-namespace.json'] "
                "returned non-zero exit status 1."
            ),
        },
    )
    resource["fencing_token"] = 47
    _validate_mirror_snapshot(
        task=task,
        candidate=candidate,
        job=job,
        jobs=jobs,
        resource=resource,
        signoff_exists=False,
        evaluation_count=0,
        namespace_authorization_count=1,
    )
