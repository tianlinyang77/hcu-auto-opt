from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hcuopt.adapters.endpoint_adjudication import LocalEndpointCampaignAdjudicator
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.cli import main
from hcuopt.contracts.endpoint_adjudication_v1 import (
    EndpointAdjudicationGroupRef,
    EndpointCampaignCreate,
    EndpointFormalAdjudicationRequest,
    endpoint_adjudication_result_hash,
)
from hcuopt.contracts.endpoint_control_v1 import EndpointAcquisitionResultRef
from hcuopt.evaluation.endpoint_adjudication import adjudicate_endpoint_campaign
from hcuopt.measurement.endpoint_models import (
    EndpointMeasurementPlan,
    EndpointWorkloadSpec,
    SignedM1EvidenceReference,
    endpoint_plan_hash,
)
from hcuopt.workers.handlers import JobHandlers


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _write_json(path: Path, value: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(encoded)
    return _hash_bytes(encoded)


def _signed_m1() -> SignedM1EvidenceReference:
    return SignedM1EvidenceReference(
        task_id=uuid4(),
        candidate_id=uuid4(),
        baseline_epoch_id=uuid4(),
        target_snapshot_id=uuid4(),
        target_id="bw20-sglang-0.5.12",
        target_fingerprint="sha256:" + "1" * 64,
        candidate_source_hash="sha256:" + "2" * 64,
        artifact_id=uuid4(),
        artifact_hash="sha256:" + "3" * 64,
        evidence_bundle_id=uuid4(),
        evidence_bundle_hash="sha256:" + "4" * 64,
        signoff_id=uuid4(),
    )


def _workload() -> EndpointWorkloadSpec:
    prompt = "The capital of France is"
    return EndpointWorkloadSpec(
        workload_id="endpoint-adjudication-fixture",
        workload_hash="sha256:" + "5" * 64,
        target_id="bw20-sglang-0.5.12",
        framework_version="0.5.12",
        source_commit="a" * 40,
        image_digest="sha256:" + "6" * 64,
        model_path="/models/fixture",
        served_model_name="fixture",
        tensor_parallel_size=1,
        closed_loop_concurrency=1,
        attention_backend="fa3",
        page_size=64,
        prompt=prompt,
        prompt_sha256=_hash_bytes(prompt.encode()),
        expected_prompt_tokens=5,
        expected_completion_tokens=8,
        sampling_seed=0,
        stream=False,
        cpu_affinity="0-1",
        numa_node=0,
    )


def _process_record(*, ordinal: int, pid: int, event: str) -> dict[str, object]:
    return {
        "schema_version": "process-lifecycle-v1",
        "event": event,
        "restart_ordinal": ordinal,
        "observer_process_id": 1,
        "process_id": pid,
        "proc_stat_line": f"{pid} (python) S 1 1 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 {pid * 100}",
        "captured_monotonic_ns": pid * 1000,
        "waitpid_result_pid": pid if event == "reaped" else None,
        "wait_status": 0 if event == "reaped" else None,
    }


def _request(
    tmp_path: Path,
    *,
    candidate_multipliers: tuple[float, ...] = (0.9,) * 8,
) -> tuple[EndpointFormalAdjudicationRequest, Path]:
    signed = _signed_m1()
    workload = _workload()
    plan = EndpointMeasurementPlan(
        run_mode="provisional",
        acquisition_order=("baseline", "candidate", "candidate", "baseline"),
        warmup_requests=1,
        measured_requests_per_acquisition=2,
        ready_timeout_seconds=300,
        request_timeout_seconds=60,
    )
    manifest: dict[str, str] = {}
    groups = []
    for group_ordinal in range(8):
        acquisitions = []
        run_id = uuid4()
        for ordinal, arm in enumerate(plan.acquisition_order):
            root = tmp_path / f"group-{group_ordinal}" / f"{ordinal:04d}-{arm}"
            pid = 10_000 + group_ordinal * 10 + ordinal
            latency = 100_000_000
            if arm == "candidate":
                latency = int(latency * candidate_multipliers[group_ordinal])
            result_hash = _write_json(
                root / "result.json",
                {
                    "status": "succeeded",
                    "completed_requests": 2,
                    "expected_requests": 2,
                    "cleanup_succeeded": True,
                    "cache_cleanup_succeeded": True,
                    "producer_verdict": None,
                    "automatic_release_allowed": False,
                },
            )
            activation_hash = _write_json(
                root / "activation.json",
                {
                    "process_id": pid,
                    "module_sha256": (
                        signed.artifact_hash if arm == "candidate" else "sha256:" + "7" * 64
                    ),
                },
            )
            cache_hash = _write_json(
                root / "cache-namespace.json",
                {
                    "empty_before_start": True,
                    "namespace_hash": "sha256:" + f"{group_ordinal * 4 + ordinal:064x}",
                },
            )
            _write_json(
                root / "process-start.json",
                _process_record(ordinal=ordinal, pid=pid, event="started"),
            )
            _write_json(
                root / "process-exit.json",
                _process_record(ordinal=ordinal, pid=pid, event="reaped"),
            )
            _write_json(root / "stop.json", {"cleanup_succeeded": True})
            _write_json(root / "cache-cleanup.json", {"removed": True})
            warmup_root = root / "warmup" / "0000"
            _write_json(warmup_root / "request.json", {"ordinal": 0})
            _write_json(warmup_root / "response.json", {"text": "Paris"})
            _write_json(
                warmup_root / "sample.json",
                {
                    "request_ordinal": 0,
                    "measured": False,
                    "succeeded": True,
                    "prompt_tokens": 5,
                    "completion_tokens": 8,
                    "error": None,
                },
            )
            for request_ordinal in range(2):
                request_root = root / "requests" / f"{request_ordinal:04d}"
                _write_json(request_root / "request.json", {"ordinal": request_ordinal})
                _write_json(request_root / "response.json", {"text": "Paris"})
                started = pid * 1_000_000 + request_ordinal * latency
                _write_json(
                    request_root / "sample.json",
                    {
                        "request_ordinal": request_ordinal,
                        "measured": True,
                        "succeeded": True,
                        "started_monotonic_ns": started,
                        "finished_monotonic_ns": started + latency,
                        "e2e_latency_ns": latency,
                        "prompt_tokens": 5,
                        "completion_tokens": 8,
                        "error": None,
                    },
                )
            for path in root.rglob("*"):
                if path.is_file():
                    manifest[path.as_posix()] = _hash_bytes(path.read_bytes())
            acquisitions.append(
                EndpointAcquisitionResultRef(
                    acquisition_ordinal=ordinal,
                    arm=arm,
                    evidence_uri=root.as_uri(),
                    result_sha256=result_hash,
                    activation_sha256=activation_hash,
                    cache_namespace_sha256=cache_hash,
                    cleanup_succeeded=True,
                )
            )
        groups.append(
            EndpointAdjudicationGroupRef(
                group_ordinal=group_ordinal,
                endpoint_run_id=run_id,
                plan_hash=endpoint_plan_hash(plan),
                acquisitions=tuple(acquisitions),
            )
        )
    manifest_path = tmp_path / "raw-evidence-hashes.json"
    manifest_hash = _write_json(manifest_path, manifest)
    return (
        EndpointFormalAdjudicationRequest(
            campaign_id=UUID("00000000-0000-0000-0000-000000000157"),
            signed_m1=signed,
            workload=workload,
            group_plan=plan,
            environment_fingerprint="sha256:" + "8" * 64,
            baseline_module_hash="sha256:" + "7" * 64,
            groups=tuple(groups),
            raw_evidence_manifest_uri=manifest_path.as_uri(),
            raw_evidence_manifest_sha256=manifest_hash,
        ),
        manifest_path,
    )


def test_endpoint_d_rereads_eight_groups_and_calls_faster(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)

    result = adjudicate_endpoint_campaign(request, allowed_roots=(tmp_path,))

    assert result.verdict == "faster"
    assert result.formal_d_adjudication is True
    assert result.automatic_release_allowed is False
    assert result.successful_groups == 8
    assert result.measured_requests == 64
    assert result.paired_latency_reduction_percent == pytest.approx(10.0)
    assert result.confidence_interval_percent is not None
    assert result.confidence_interval_percent[0] > 9.99
    assert result.verified_file_count == 8 * 4 * 16 + 1


def test_endpoint_d_calls_noisy_campaign_inconclusive(tmp_path: Path) -> None:
    request, _ = _request(
        tmp_path,
        candidate_multipliers=(0.9, 1.1, 0.92, 1.08, 0.94, 1.06, 0.96, 1.04),
    )

    result = adjudicate_endpoint_campaign(request, allowed_roots=(tmp_path,))

    assert result.verdict == "inconclusive"
    assert result.confidence_interval_percent is not None
    assert result.confidence_interval_percent[0] < 0 < result.confidence_interval_percent[1]


def test_endpoint_adjudication_request_replays_json_signed_m1_uuids(
    tmp_path: Path,
) -> None:
    request, _ = _request(tmp_path)

    replayed = EndpointFormalAdjudicationRequest.model_validate(
        request.model_dump(mode="json")
    )

    assert replayed == request


def test_endpoint_d_worker_handler_uses_root_bound_real_adapter(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    profile = "endpoint-formal-adjudicator-v1"
    adapter = LocalEndpointCampaignAdjudicator(
        profile=profile,
        allowed_evidence_roots=(tmp_path,),
    )
    handlers = JobHandlers(
        AdapterRegistry(profile=profile, endpoint_campaign_adjudicator=adapter)
    )
    payload = request.model_dump(mode="json")
    payload["_job_context"] = {"untrusted": "not part of the frozen request"}

    result = handlers.handle("endpoint_adjudicate", payload)

    assert result["verdict"] == "faster"
    assert result["formal_d_adjudication"] is True
    assert result["automatic_release_allowed"] is False
    assert endpoint_adjudication_result_hash(result) == endpoint_adjudication_result_hash(
        adjudicate_endpoint_campaign(request, allowed_roots=(tmp_path,))
    )


def test_endpoint_d_fails_closed_after_sample_tampering(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    sample = tmp_path / "group-3" / "0001-candidate" / "requests" / "0000" / "sample.json"
    sample.write_text("{}", encoding="utf-8")

    result = adjudicate_endpoint_campaign(request, allowed_roots=(tmp_path,))

    assert result.verdict == "invalid"
    assert result.successful_groups == 0
    assert result.measured_requests == 0
    assert result.baseline_mean_ns is None
    assert "Hash differs" in result.reason


def test_endpoint_d_rejects_reused_process_identity(tmp_path: Path) -> None:
    request, manifest_path = _request(tmp_path)
    source = tmp_path / "group-0" / "0000-baseline" / "process-start.json"
    target = tmp_path / "group-0" / "0001-candidate" / "process-start.json"
    target.write_bytes(source.read_bytes())
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[target.as_posix()] = _hash_bytes(target.read_bytes())
    manifest_hash = _write_json(manifest_path, manifest)
    request = request.model_copy(
        update={"raw_evidence_manifest_sha256": manifest_hash}, deep=True
    )

    result = adjudicate_endpoint_campaign(request, allowed_roots=(tmp_path,))

    assert result.verdict == "invalid"
    assert "lifecycle is invalid" in result.reason or "reused" in result.reason


def test_endpoint_adjudication_cli_publishes_machine_readable_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request, _ = _request(tmp_path)
    request_path = tmp_path / "adjudication-request.json"
    request_path.write_text(request.model_dump_json(indent=2), encoding="utf-8")

    exit_code = main(
        [
            "endpoint-adjudicate",
            str(request_path),
            "--allow-root",
            str(tmp_path),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["verdict"] == "faster"
    assert output["formal_d_adjudication"] is True
    assert output["automatic_release_allowed"] is False


def test_endpoint_campaign_create_requires_eight_local_group_bindings() -> None:
    run_ids = tuple(uuid4() for _ in range(8))
    request = EndpointCampaignCreate(
        name="formal endpoint campaign",
        endpoint_run_ids=run_ids,
        raw_evidence_manifest_uri="file:///evidence/raw-hashes.json",
        raw_evidence_manifest_sha256="sha256:" + "b" * 64,
        idempotency_key="formal-endpoint-campaign-v1",
    )

    assert request.endpoint_run_ids == run_ids
    assert request.automatic_release_allowed is False
    with pytest.raises(ValueError, match="eight distinct"):
        EndpointCampaignCreate(
            name=request.name,
            endpoint_run_ids=(run_ids[0],) * 8,
            raw_evidence_manifest_uri=request.raw_evidence_manifest_uri,
            raw_evidence_manifest_sha256=request.raw_evidence_manifest_sha256,
            idempotency_key=request.idempotency_key,
        )
    with pytest.raises(ValueError, match="local file URI"):
        EndpointCampaignCreate(
            name=request.name,
            endpoint_run_ids=run_ids,
            raw_evidence_manifest_uri="https://example.invalid/raw-hashes.json",
            raw_evidence_manifest_sha256=request.raw_evidence_manifest_sha256,
            idempotency_key=request.idempotency_key,
        )
