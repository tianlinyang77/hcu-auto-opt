# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Read-only replay of BW20 F1 evidence, not new hardware acceptance or signoff."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from hcuopt.contracts.platform_v1 import TargetSpec
from hcuopt.evaluation.sglang_smoke import normalize_response
from hcuopt.source_hash import file_uri_to_path
from hcuopt.targets import target_fingerprint


class EvidenceVerificationError(ValueError):
    """A stable, credential-free fail-closed audit error."""


def require(condition: object, code: str) -> None:
    if not condition:
        raise EvidenceVerificationError(code)


def verify_bw20_evidence(directory: Path, *, receipt_sha256: str) -> dict:
    """Require an out-of-band trusted receipt digest; do not modify any evidence.

    The caller controls the trust anchor. Computing it from an untrusted bundle
    does not establish authenticity. All source paths must stay inside this run.
    """
    try:
        return _verify(directory, receipt_sha256)
    except EvidenceVerificationError:
        raise
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
        raise EvidenceVerificationError("malformed_or_incomplete_evidence") from exc


def _verify(directory: Path, receipt_sha256: str) -> dict:
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", receipt_sha256), "invalid_receipt_digest")
    root = directory.absolute()
    require(root.resolve(strict=True) == root and root.is_dir(), "redirected_run_directory")

    def safe_path(path: Path) -> Path:
        require(path.is_absolute() and path.is_relative_to(root), "evidence_path_outside_run")
        require(".." not in path.parts, "evidence_path_traversal")
        for ancestor in (path, *path.parents):
            if ancestor == root:
                break
            require(not ancestor.is_symlink(), "redirected_evidence_path")
        resolved = path.resolve(strict=True)
        require(resolved == path and resolved.is_relative_to(root), "redirected_evidence_path")
        require(path.is_file(), "evidence_not_file")
        return path

    def digest(path: Path) -> str:
        value = hashlib.sha256()
        with safe_path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                value.update(chunk)
        return "sha256:" + value.hexdigest()

    def read(path: Path):
        return json.loads(safe_path(path).read_text(encoding="utf-8"))

    receipt_path = root / "independent-readback.json"
    require(digest(receipt_path) == receipt_sha256, "receipt_digest_mismatch")
    trusted = read(receipt_path)
    require(trusted["api_walking_skeleton_verified"] is True, "receipt_not_verified")
    require(trusted["task_state"] == "awaiting_signoff", "receipt_not_awaiting_signoff")
    for key in (
        "full_system_acceptance",
        "candidate_activation",
        "stage0_accepted",
        "signoff_performed",
        "automatic_release_allowed",
    ):
        require(trusted[key] is False, "receipt_scope_expansion")
    for key, relative in (
        ("plan_sha256", "pair/plan.json"),
        ("api_summary_sha256", "api-final-summary.json"),
        ("database_readback_sha256", "database-readback.json"),
        ("evidence_sha256", "pair/evidence-bundle.json"),
    ):
        require(digest(root / relative) == trusted[key], "trusted_document_digest_mismatch")
    pair = root / "pair"
    scope = read(root / "run-scope.json")
    created = read(root / "api-create.json")
    summary = read(root / "api-final-summary.json")
    db = read(root / "database-readback.json")
    acceptance = read(root / "acceptance-result.json")
    plan = read(pair / "plan.json")
    require(
        created["status"] == 201 and created["body"]["task_id"] == scope["task_id"],
        "api_creation_mismatch",
    )
    require(summary["task"]["task_id"] == scope["task_id"], "summary_task_mismatch")
    require(
        summary["task"]["state"] == "awaiting_signoff" and summary["adapter_mode"] == "real",
        "summary_not_real_awaiting_signoff",
    )
    require(
        target_fingerprint(TargetSpec.model_validate(plan["target"]))
        == scope["target_fingerprint"],
        "target_fingerprint_mismatch",
    )
    require(
        db["job"]["state"] == "succeeded" and db["job"]["workflow_advanced_at"], "job_not_completed"
    )
    require(
        db["job"]["task_id"] == scope["task_id"] and db["resource"]["state"] == "available",
        "resource_or_task_mismatch",
    )
    result = db["job"]["result"]
    require(
        not result["synthetic"] and result["evaluation"]["passed"] is True,
        "evaluation_not_real_pass",
    )
    require(
        result["evaluation"] == read(pair / "evaluation-run.json"), "evaluation_readback_mismatch"
    )
    require(result["evidence"] == read(pair / "evidence-bundle.json"), "evidence_readback_mismatch")
    manifest = read(pair / "sha256sums.json")
    required_files = {
        "plan.json",
        "evaluation-run.json",
        "evidence-bundle.json",
        "transport-hashes.json",
    }
    for variant in ("baseline", "noop"):
        required_files.update(f"{variant}/{name}" for name in ("response.json", "result.json"))
    require(required_files <= manifest.keys(), "incomplete_hash_manifest")
    for relative, expected_digest in manifest.items():
        path = safe_path(pair / relative)
        require(
            path.is_relative_to(pair) and digest(path) == expected_digest, "file_digest_mismatch"
        )
    outputs = []
    containers = []
    for index, variant in enumerate(("baseline", "noop")):
        request = plan["requests"][index]
        entry = result["executions"][index]
        require(
            entry["variant"] == variant and entry["execution_request"] == request,
            "execution_request_mismatch",
        )
        require(entry["execution_result"]["exit_code"] == 0, "execution_nonzero_exit")
        require(entry["execution_result"]["status"] == "succeeded", "execution_not_succeeded")
        response = read(pair / variant / "response.json")
        require(response["http_status"] == 200, "response_not_http_200")
        output = normalize_response(response["body_json"]).model_dump(mode="json")
        raw_result = read(pair / variant / "result.json")
        require(raw_result["normalized_output"] == output, "normalized_output_mismatch")
        require(
            raw_result["status"] == "succeeded" and raw_result["cleanup_succeeded"],
            "raw_result_not_clean_success",
        )
        require(
            output["text"] == " Paris. It is the largest city in", "frozen_fixture_text_mismatch"
        )
        require(
            output["prompt_tokens"] == 5 and output["completion_tokens"] == 8,
            "frozen_fixture_token_mismatch",
        )
        transported = read(pair / "transport-hashes.json")[variant]
        require(
            {"response.json", "result.json"} <= transported.keys(), "incomplete_transport_hashes"
        )
        for name, expected_digest in transported.items():
            require(digest(pair / variant / name) == expected_digest, "transport_digest_mismatch")
        for field in ("stdout", "stderr"):
            path = safe_path(file_uri_to_path(entry["execution_result"][field + "_uri"]))
            require(
                digest(path) == entry["execution_result"]["metadata"][field + "_sha256"],
                "execution_log_digest_mismatch",
            )
        identifier = request["request_id"].replace("-", "")
        live = read(root / ("hcuopt-" + identifier + "-live.json"))
        host = live["HostConfig"]
        require(
            live["State"]["Running"] is True and live["User"] == "65534:65534",
            "container_user_or_state_mismatch",
        )
        require(
            live["Image"] == plan["target"]["inference_image"]["image_id"],
            "container_image_mismatch",
        )
        require(
            host["NetworkMode"] == "none"
            and host["ReadonlyRootfs"] is True
            and host["Privileged"] is False,
            "container_isolation_mismatch",
        )
        require(
            host["CpusetCpus"] == "64-79" and host["CpusetMems"] == "4",
            "container_cpu_numa_mismatch",
        )
        require(
            host["Memory"] == host["MemorySwap"] == 17179869184 and host["PidsLimit"] == 512,
            "container_resource_limits_mismatch",
        )
        require(host["CapDrop"] == ["ALL"] and host["Init"], "container_caps_or_init_mismatch")
        require(
            any("no-new-privileges" in option for option in host["SecurityOpt"]),
            "container_privilege_guard_missing",
        )
        require(
            {device["PathOnHost"] for device in host["Devices"]}
            == {"/dev/kfd", "/dev/dri/renderD135"},
            "container_device_mapping_mismatch",
        )
        expected = {(m["source"], m["target"], m["read_only"]) for m in request["mounts"]}
        observed = {(m["Source"], m["Target"], m.get("ReadOnly", False)) for m in host["Mounts"]}
        require(expected == observed, "container_mount_mismatch")
        require(
            sum(m["RW"] for m in live["Mounts"] if m["Type"] == "bind") == 1,
            "container_writable_bind_count_mismatch",
        )
        events = [
            json.loads(line)
            for line in safe_path(root / (identifier + "-events.jsonl"))
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        require(
            any(
                e["Action"] == "die" and e["Actor"]["Attributes"]["exitCode"] == "0" for e in events
            ),
            "container_successful_exit_missing",
        )
        require(any(e["Action"] == "destroy" for e in events), "container_destroy_missing")
        require(
            all(e["Actor"]["ID"] == live["Id"] for e in events), "container_event_identity_mismatch"
        )
        cleanup = entry["cleanup_evidence"]
        require(
            cleanup["fence"]["fenced"] is True and cleanup["health"]["healthy"] is True,
            "cleanup_not_healthy_or_fenced",
        )
        require(cleanup["health"]["host_state"]["kfd_pids"] == [], "cleanup_kfd_not_empty")
        outputs.append(output)
        containers.append(live["Id"])
    require(
        outputs[0] == outputs[1] and len(set(containers)) == 2, "pair_not_distinct_and_equivalent"
    )
    require(
        acceptance["post_host"]["busy"] == 0 and acceptance["post_host"]["kfd_pids"] == [],
        "post_host_not_idle",
    )
    require(
        acceptance["signoff_performed"] is False and acceptance["stage0_accepted"] is False,
        "acceptance_scope_expansion",
    )

    require(scope["task_id"] == trusted["task_id"], "receipt_task_mismatch")
    require(outputs[0] == trusted["output"], "receipt_output_mismatch")
    require(containers == trusted["container_ids"], "receipt_containers_mismatch")
    require(result["synthetic"] is False, "synthetic_evidence")
    require(len(plan["requests"]) == len(result["executions"]) == 2, "execution_count_mismatch")
    for key in ("candidate_activation", "automatic_release_allowed"):
        require(acceptance[key] is False, "acceptance_scope_expansion")
    require(acceptance["evaluation_passed"] is True, "acceptance_evaluation_failed")
    require(acceptance["worker_completed"] is True, "acceptance_worker_incomplete")
    require(acceptance["task_state"] == "awaiting_signoff", "acceptance_state_mismatch")
    return {
        "schema": "bw20-evidence-replay-v1",
        "historical_evidence_verified": True,
        "task_id": scope["task_id"],
        "persisted_task_state": "awaiting_signoff",
        "receipt_sha256": receipt_sha256,
        "live_database_checked": False,
        "new_hcu_execution": False,
        "signoff_performed": False,
        "candidate_activation": False,
        "stage0_accepted": False,
        "automatic_release_allowed": False,
        "performance_conclusion": "not_measured",
    }
