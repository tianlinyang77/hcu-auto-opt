# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Synthetic local fixtures; these tests never establish hardware acceptance."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from hcuopt.cli import main
from hcuopt.deployment.bw20_evidence import EvidenceVerificationError, verify_bw20_evidence
from hcuopt.evaluation.sglang_smoke import normalize_response
from hcuopt.targets import load_target, target_fingerprint


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def sha(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "run"
    target = load_target(
        Path(__file__).resolve().parents[2] / "config/targets/bw20-sglang-0.5.12.yaml"
    )
    task = str(uuid4())
    body = {
        "text": " Paris. It is the largest city in",
        "meta_info": {
            "prompt_tokens": 5,
            "completion_tokens": 8,
            "finish_reason": {"type": "length"},
        },
    }
    output = normalize_response(body).model_dump(mode="json")
    plan = {"target": target.model_dump(mode="json"), "requests": []}
    result = {
        "synthetic": False,
        "evaluation": {"passed": True},
        "evidence": {"fixture": "not real hardware"},
        "executions": [],
    }
    containers = []
    transport = {}
    for variant in ("baseline", "noop"):
        identifier = uuid4().hex
        container = uuid4().hex
        containers.append(container)
        request = {
            "request_id": identifier,
            "mounts": [{"source": "/fixture", "target": "/output", "read_only": False}],
        }
        plan["requests"].append(request)
        save(root / "pair" / variant / "response.json", {"http_status": 200, "body_json": body})
        save(
            root / "pair" / variant / "result.json",
            {"normalized_output": output, "status": "succeeded", "cleanup_succeeded": True},
        )
        execution = {"exit_code": 0, "status": "succeeded", "metadata": {}}
        for field in ("stdout", "stderr"):
            path = root / (variant + "-" + field + ".txt")
            path.write_text("fixture", encoding="utf-8")
            execution[field + "_uri"] = path.as_uri()
            execution["metadata"][field + "_sha256"] = sha(path)
        result["executions"].append(
            {
                "variant": variant,
                "execution_request": request,
                "execution_result": execution,
                "cleanup_evidence": {
                    "fence": {"fenced": True},
                    "health": {"healthy": True, "host_state": {"kfd_pids": []}},
                },
            }
        )
        save(
            root / ("hcuopt-" + identifier + "-live.json"),
            {
                "Id": container,
                "State": {"Running": True},
                "User": "65534:65534",
                "Image": target.inference_image.image_id,
                "Mounts": [{"Type": "bind", "RW": True}],
                "HostConfig": {
                    "NetworkMode": "none",
                    "ReadonlyRootfs": True,
                    "Privileged": False,
                    "CpusetCpus": "64-79",
                    "CpusetMems": "4",
                    "Memory": 17179869184,
                    "MemorySwap": 17179869184,
                    "PidsLimit": 512,
                    "CapDrop": ["ALL"],
                    "Init": True,
                    "SecurityOpt": ["no-new-privileges"],
                    "Devices": [{"PathOnHost": x} for x in ("/dev/kfd", "/dev/dri/renderD135")],
                    "Mounts": [{"Source": "/fixture", "Target": "/output", "ReadOnly": False}],
                },
            },
        )
        (root / (identifier + "-events.jsonl")).write_text(
            "\n".join(
                json.dumps(
                    {"Action": action, "Actor": {"ID": container, "Attributes": {"exitCode": "0"}}}
                )
                for action in ("die", "destroy")
            ),
            encoding="utf-8",
        )
        transport[variant] = {
            name: sha(root / "pair" / variant / name) for name in ("response.json", "result.json")
        }
    save(root / "pair/transport-hashes.json", transport)
    save(root / "pair/plan.json", plan)
    save(root / "pair/evaluation-run.json", result["evaluation"])
    save(root / "pair/evidence-bundle.json", result["evidence"])
    save(
        root / "run-scope.json", {"task_id": task, "target_fingerprint": target_fingerprint(target)}
    )
    save(root / "api-create.json", {"status": 201, "body": {"task_id": task}})
    save(
        root / "api-final-summary.json",
        {"task": {"task_id": task, "state": "awaiting_signoff"}, "adapter_mode": "real"},
    )
    save(
        root / "database-readback.json",
        {
            "job": {
                "state": "succeeded",
                "task_id": task,
                "workflow_advanced_at": "fixture",
                "result": result,
            },
            "resource": {"state": "available"},
        },
    )
    save(
        root / "acceptance-result.json",
        {
            "post_host": {"busy": 0, "kfd_pids": []},
            "signoff_performed": False,
            "stage0_accepted": False,
            "candidate_activation": False,
            "automatic_release_allowed": False,
            "evaluation_passed": True,
            "worker_completed": True,
            "task_state": "awaiting_signoff",
        },
    )
    save(
        root / "pair/sha256sums.json",
        {
            str(path.relative_to(root / "pair")).replace("\\", "/"): sha(path)
            for path in (root / "pair").rglob("*")
            if path.is_file()
        },
    )
    receipt = {
        "api_walking_skeleton_verified": True,
        "task_state": "awaiting_signoff",
        "task_id": task,
        "output": output,
        "container_ids": containers,
    }
    for key in (
        "full_system_acceptance",
        "candidate_activation",
        "stage0_accepted",
        "signoff_performed",
        "automatic_release_allowed",
    ):
        receipt[key] = False
    for key, path in (
        ("plan_sha256", "pair/plan.json"),
        ("api_summary_sha256", "api-final-summary.json"),
        ("database_readback_sha256", "database-readback.json"),
        ("evidence_sha256", "pair/evidence-bundle.json"),
    ):
        receipt[key] = sha(root / path)
    save(root / "independent-readback.json", receipt)
    return root, sha(root / "independent-readback.json")


def test_full_replay_read_only(bundle):
    root, pin = bundle
    before = {p: sha(p) for p in root.rglob("*") if p.is_file()}
    report = verify_bw20_evidence(root, receipt_sha256=pin)
    assert report["historical_evidence_verified"] is True
    for key in (
        "live_database_checked",
        "new_hcu_execution",
        "signoff_performed",
        "candidate_activation",
        "automatic_release_allowed",
    ):
        assert report[key] is False
    assert {p: sha(p) for p in root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize(
    "relative",
    [
        "api-final-summary.json",
        "database-readback.json",
        "pair/plan.json",
        "pair/evidence-bundle.json",
        "pair/baseline/response.json",
        "pair/noop/result.json",
        "baseline-stdout.txt",
        "independent-readback.json",
    ],
)
def test_tamper_rejected(bundle, relative):
    root, pin = bundle
    (root / relative).write_text("{}", encoding="utf-8")
    with pytest.raises(EvidenceVerificationError):
        verify_bw20_evidence(root, receipt_sha256=pin)


def test_missing_file_rejected(bundle):
    root, pin = bundle
    (root / "pair/noop/response.json").unlink()
    with pytest.raises(EvidenceVerificationError):
        verify_bw20_evidence(root, receipt_sha256=pin)


@pytest.mark.parametrize("escape", ["../../outside.json", "../run-scope.json"])
def test_manifest_traversal_rejected(bundle, escape):
    root, pin = bundle
    path = root / "pair/sha256sums.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest[escape] = "sha256:" + "0" * 64
    save(path, manifest)
    with pytest.raises(EvidenceVerificationError, match="path"):
        verify_bw20_evidence(root, receipt_sha256=pin)


def test_empty_manifest_cannot_skip_hash_checks(bundle):
    root, pin = bundle
    save(root / "pair/sha256sums.json", {})
    with pytest.raises(EvidenceVerificationError, match="incomplete_hash_manifest"):
        verify_bw20_evidence(root, receipt_sha256=pin)


@pytest.mark.parametrize(
    "field,value",
    [
        ("Privileged", True),
        ("ReadonlyRootfs", "true"),
        ("NetworkMode", "host"),
        ("CpusetCpus", "0-15"),
        ("Devices", []),
    ],
)
def test_recorded_container_scope_drift_rejected(bundle, field, value):
    root, pin = bundle
    path = next(root.glob("hcuopt-*-live.json"))
    live = json.loads(path.read_text(encoding="utf-8"))
    live["HostConfig"][field] = value
    save(path, live)
    with pytest.raises(EvidenceVerificationError, match="container"):
        verify_bw20_evidence(root, receipt_sha256=pin)


def test_missing_destroy_event_rejected(bundle):
    root, pin = bundle
    path = next(root.glob("*-events.jsonl"))
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(lines[0], encoding="utf-8")
    with pytest.raises(EvidenceVerificationError, match="destroy"):
        verify_bw20_evidence(root, receipt_sha256=pin)


@pytest.mark.parametrize(
    "field",
    ["candidate_activation", "automatic_release_allowed", "signoff_performed", "stage0_accepted"],
)
def test_acceptance_scope_cannot_expand(bundle, field):
    root, pin = bundle
    path = root / "acceptance-result.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record[field] = True
    save(path, record)
    with pytest.raises(EvidenceVerificationError, match="scope_expansion"):
        verify_bw20_evidence(root, receipt_sha256=pin)


def test_symlink_rejected(bundle):
    root, pin = bundle
    path = root / "pair/noop/response.json"
    target = path.with_suffix(".original")
    path.rename(target)
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(EvidenceVerificationError, match="redirected"):
        verify_bw20_evidence(root, receipt_sha256=pin)


def test_invalid_pin_and_cli_error_redaction(bundle, capsys):
    root, _ = bundle
    assert main(["bw20-evidence-verify", str(root), "--receipt-sha256", "secret-value"]) == 2
    output = capsys.readouterr().out
    assert "invalid_receipt_digest" in output and "secret-value" not in output


@pytest.mark.parametrize("optimized", [False, True])
def test_fresh_cli_with_optimization_cannot_bypass_checks(bundle, optimized):
    root, pin = bundle
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    command = [
        sys.executable,
        *(["-O"] if optimized else []),
        "-m",
        "hcuopt",
        "bw20-evidence-verify",
        str(root),
        "--receipt-sha256",
        pin,
    ]
    passed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert passed.returncode == 0, passed.stderr + passed.stdout
    (root / "pair/noop/result.json").write_text("{}", encoding="utf-8")
    failed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert failed.returncode == 2
    assert json.loads(failed.stdout)["historical_evidence_verified"] is False
