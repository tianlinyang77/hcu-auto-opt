from __future__ import annotations

import hashlib
from uuid import UUID

import pytest
from pydantic import ValidationError

from hcuopt.domain.enums import LeaseScope
from hcuopt.measurement.endpoint_models import (
    EndpointAcquisitionEvidence,
    EndpointActivationEvidence,
    EndpointCleanupEvidence,
    EndpointLifecycleEvidence,
    EndpointMeasurementPlan,
    EndpointRequestSample,
    EndpointValidationBinding,
    EndpointValidationEvidence,
    EndpointWorkloadSpec,
    SignedM1EvidenceReference,
    endpoint_plan_hash,
)
from hcuopt.measurement.models import (
    ProcessLifecycleRecordV2,
    RawEvidenceFileV2,
    Stage0AdapterProvenance,
    Stage0LeaseBinding,
)


def _uuid(value: int) -> UUID:
    return UUID(int=value)


def _hash(value: int) -> str:
    return f"sha256:{value:064x}"


def _raw(value: int) -> RawEvidenceFileV2:
    return RawEvidenceFileV2(uri=f"file:///evidence/{value}.json", sha256=_hash(value))


def _plan() -> EndpointMeasurementPlan:
    return EndpointMeasurementPlan(
        run_mode="provisional",
        acquisition_order=("baseline", "candidate", "candidate", "baseline"),
        warmup_requests=1,
        measured_requests_per_acquisition=1,
        ready_timeout_seconds=300,
        request_timeout_seconds=60,
    )


def _acquisition(ordinal: int, arm: str) -> EndpointAcquisitionEvidence:
    process_id = 1000 + ordinal
    started = ProcessLifecycleRecordV2(
        event="started",
        restart_ordinal=ordinal,
        observer_process_id=1,
        process_id=process_id,
        proc_stat_line=f"{process_id} (sglang) S 1 1 1 0",
        captured_monotonic_ns=100 + ordinal * 10,
    )
    reaped = ProcessLifecycleRecordV2(
        event="reaped",
        restart_ordinal=ordinal,
        observer_process_id=1,
        process_id=process_id,
        proc_stat_line=started.proc_stat_line,
        captured_monotonic_ns=109 + ordinal * 10,
        waitpid_result_pid=process_id,
        wait_status=0,
    )
    base = 100 + ordinal * 20
    candidate = arm == "candidate"
    return EndpointAcquisitionEvidence(
        acquisition_ordinal=ordinal,
        arm=arm,
        lifecycle=EndpointLifecycleEvidence(
            process_id=process_id,
            process_start_token=f"start-{ordinal}",
            started=started,
            started_raw=_raw(base),
            reaped=reaped,
            reaped_raw=_raw(base + 1),
            ready_raw=_raw(base + 2),
            server_log=_raw(base + 3),
        ),
        activation=EndpointActivationEvidence(
            arm=arm,
            image_digest=_hash(10),
            activation_mode="startup_overlay" if candidate else "baseline",
            loaded_artifact_hash=_hash(12) if candidate else None,
            import_attestation=_raw(base + 4) if candidate else None,
            cache_namespace_hash=_hash(base + 5),
            cache_namespace_evidence=_raw(base + 6),
            cache_empty_before_start=True,
        ),
        warmup_raw=(_raw(base + 7),),
        requests=(
            EndpointRequestSample(
                request_ordinal=0,
                succeeded=True,
                http_status=200,
                started_monotonic_ns=1_000_000,
                finished_monotonic_ns=1_100_000,
                e2e_latency_ns=100_000,
                prompt_tokens=5,
                completion_tokens=8,
                finish_reason="length",
                raw_request=_raw(base + 8),
                raw_response=_raw(base + 9),
            ),
        ),
        cleanup=EndpointCleanupEvidence(
            process_reaped=True,
            container_absent=True,
            resource_restored=True,
            fence_raw=_raw(base + 10),
            health_raw=_raw(base + 11),
            cleanup_raw=_raw(base + 12),
        ),
    )


def _evidence() -> EndpointValidationEvidence:
    plan = _plan()
    signed = SignedM1EvidenceReference(
        task_id=_uuid(1),
        candidate_id=_uuid(2),
        baseline_epoch_id=_uuid(3),
        target_snapshot_id=_uuid(4),
        target_id="bw20-sglang-0.5.12",
        target_fingerprint=_hash(17),
        candidate_source_hash=_hash(11),
        artifact_id=_uuid(5),
        artifact_hash=_hash(12),
        evidence_bundle_id=_uuid(6),
        evidence_bundle_hash=_hash(13),
        signoff_id=_uuid(7),
    )
    return EndpointValidationEvidence(
        binding=EndpointValidationBinding(
            endpoint_run_id=_uuid(8),
            signed_m1=signed,
            adapter_profile="bw20-sglang-endpoint-v1",
            environment_fingerprint=_hash(14),
            lease=Stage0LeaseBinding(
                lease_id=_uuid(9),
                lease_scope=LeaseScope.EXCLUSIVE,
                resource_id="hcu-7",
                fencing_token=51,
            ),
        ),
        workload=EndpointWorkloadSpec(
            workload_id="bw20-sglang-endpoint-c1-v1",
            workload_hash=_hash(15),
            target_id="bw20-sglang-0.5.12",
            framework_version="0.5.12+das.opt1",
            source_commit="a" * 40,
            image_digest=_hash(10),
            model_path="/models/frozen",
            served_model_name="frozen-model",
            attention_backend="fa3",
            page_size=64,
            prompt="The capital of France is",
            prompt_sha256="sha256:"
            + hashlib.sha256(b"The capital of France is").hexdigest(),
            expected_prompt_tokens=5,
            expected_completion_tokens=8,
            sampling_seed=0,
            ignore_eos=True,
            stream=False,
            cpu_affinity="64-79",
            numa_node=4,
        ),
        plan=plan,
        plan_hash=endpoint_plan_hash(plan),
        acquisitions=tuple(
            _acquisition(ordinal, arm)
            for ordinal, arm in enumerate(plan.acquisition_order)
        ),
        adapter_provenance=(
            Stage0AdapterProvenance(
                profile="bw20-sglang-endpoint-v1",
                capability="endpoint_measurement_runner",
                adapter_name="sglang-endpoint-runner",
                adapter_version="1",
                implementation_kind="real",
                source_commit="b" * 40,
            ),
        ),
        evidence_index=_raw(999),
        automatic_release_allowed=False,
    )


def _revalidate(evidence: EndpointValidationEvidence, **updates: object) -> None:
    raw = evidence.model_dump(mode="python")
    raw.update(updates)
    EndpointValidationEvidence.model_validate(raw)


def test_valid_provisional_endpoint_evidence_is_non_releasing() -> None:
    evidence = _evidence()

    assert evidence.plan.run_mode == "provisional"
    assert len(evidence.acquisitions) == 4
    assert evidence.producer_verdict is None
    assert evidence.automatic_release_allowed is False


def test_plan_rejects_incomplete_or_misordered_acquisitions() -> None:
    with pytest.raises(ValidationError, match="complete ABBA"):
        EndpointMeasurementPlan(
            run_mode="formal",
            acquisition_order=("baseline", "candidate"),
            warmup_requests=1,
            measured_requests_per_acquisition=1,
            ready_timeout_seconds=300,
            request_timeout_seconds=60,
        )
    with pytest.raises(ValidationError, match="Baseline-Candidate-Candidate-Baseline"):
        EndpointMeasurementPlan(
            run_mode="formal",
            acquisition_order=("baseline", "candidate", "baseline", "candidate"),
            warmup_requests=1,
            measured_requests_per_acquisition=1,
            ready_timeout_seconds=300,
            request_timeout_seconds=60,
        )


def test_any_failed_request_invalidates_the_whole_acquisition() -> None:
    valid = _acquisition(0, "baseline")
    failed = valid.requests[0].model_copy(
        update={
            "succeeded": False,
            "http_status": 500,
            "prompt_tokens": None,
            "completion_tokens": None,
            "finish_reason": None,
            "error_code": "http_500",
        }
    )
    raw = valid.model_dump(mode="python")
    raw["requests"] = (failed,)
    with pytest.raises(ValidationError, match="fails closed"):
        EndpointAcquisitionEvidence.model_validate(raw)


@pytest.mark.parametrize("reused", ["process", "cache"])
def test_acquisitions_require_fresh_process_and_cache(reused: str) -> None:
    evidence = _evidence()
    acquisitions = list(evidence.acquisitions)
    first = acquisitions[0]
    second = acquisitions[1]
    if reused == "process":
        second = second.model_copy(update={"lifecycle": first.lifecycle})
    else:
        activation = second.activation.model_copy(
            update={"cache_namespace_hash": first.activation.cache_namespace_hash}
        )
        second = second.model_copy(update={"activation": activation})
    acquisitions[1] = second
    with pytest.raises(ValidationError, match=f"fresh {reused}"):
        _revalidate(evidence, acquisitions=tuple(acquisitions))


def test_candidate_must_load_the_signed_artifact() -> None:
    evidence = _evidence()
    acquisitions = list(evidence.acquisitions)
    candidate = acquisitions[1]
    activation = candidate.activation.model_copy(update={"loaded_artifact_hash": _hash(777)})
    acquisitions[1] = candidate.model_copy(update={"activation": activation})
    with pytest.raises(ValidationError, match="loaded another Artifact"):
        _revalidate(evidence, acquisitions=tuple(acquisitions))


def test_workload_cannot_cross_the_signed_m1_target() -> None:
    evidence = _evidence()
    workload = evidence.workload.model_copy(update={"target_id": "another-target"})
    with pytest.raises(ValidationError, match="signed M1 target"):
        _revalidate(evidence, workload=workload)


def test_token_mismatch_and_release_authority_are_rejected() -> None:
    evidence = _evidence()
    workload = evidence.workload.model_copy(update={"expected_completion_tokens": 9})
    with pytest.raises(ValidationError, match="token counts"):
        _revalidate(evidence, workload=workload)
    with pytest.raises(ValidationError, match="False"):
        _revalidate(evidence, automatic_release_allowed=True)


def test_streaming_workload_requires_ttft_and_tpot_for_every_request() -> None:
    evidence = _evidence()
    workload = evidence.workload.model_copy(update={"stream": True})
    with pytest.raises(ValidationError, match="streaming metrics"):
        _revalidate(evidence, workload=workload)
