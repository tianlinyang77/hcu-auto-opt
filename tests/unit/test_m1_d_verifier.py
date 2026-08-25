# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from uuid import uuid4

import pytest
from pydantic import ValidationError

from hcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    ArtifactManifest,
    SourceSnapshot,
)
from hcuopt.contracts.v1 import ManualCandidateAdjudicationResult
from hcuopt.domain.enums import ManualCandidateVerdict
from hcuopt.evaluation.evidence_reader import EvidenceReadError, HashedEvidenceReader
from hcuopt.evaluation.m1_protocol import (
    M1CorrectnessCase,
    M1HotspotCorrectnessSpec,
    M1InputExpectation,
    M1OutputSpec,
    M1ProtocolError,
    M1TensorSpec,
    load_m1_protocol,
    load_registered_m1_protocol,
    m1_hotspot_spec_sha256,
)
from hcuopt.evaluation.m1_reporting import (
    M1_SIGNOFF_WARNING,
    M1AdjudicationContext,
    build_m1_adjudication_result,
    write_m1_signoff_report,
)
from hcuopt.evaluation.m1_verifier import (
    M1CorrectnessEvidenceReference,
    M1CorrectnessVerifier,
    M1PerformanceEvidenceReference,
    M1PerformanceVerificationContext,
    M1PerformanceVerifier,
    M1VerificationContext,
)
from hcuopt.evaluation.stage0_protocol import load_registered_stage0_protocol
from hcuopt.measurement.evidence import canonical_json_bytes, write_evidence, write_evidence_bytes
from hcuopt.measurement.m1_harness import M1TrustedMeasurementHarness
from hcuopt.measurement.m1_models import M1MeasurementEvidence
from hcuopt.targets import load_target, target_fingerprint
from tests.unit.test_m1_measurement import (
    TARGET_PATH,
    FixtureCleaner,
    FixtureClock,
    FixtureLifecycle,
    FixtureTelemetry,
    FixtureTimer,
    FixtureWorkload,
)

SHA_A = "sha256:" + "a" * 64
PROFILE = "m1-scripted-real"


class _PortableReader(HashedEvidenceReader):
    def _secure_read(self, path: Path) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise EvidenceReadError("evidence_not_regular", str(path))
        encoded = path.read_bytes()
        if len(encoded) > self.max_bytes:
            raise EvidenceReadError("evidence_too_large", str(path))
        return encoded


def _provenance(capability: str) -> AdapterProvenance:
    return AdapterProvenance(
        profile=PROFILE,
        capability=capability,
        adapter_name=f"Scripted{capability.title()}",
        adapter_version="1",
        implementation_kind="real",
    )


def _input_tensors(seed: int, special: str) -> list[dict[str, object]]:
    if special == "nan":
        values: list[object] = ["nan", 1.0]
    elif special == "positive_inf":
        values = ["positive_inf", 1.0]
    else:
        values = [float(seed), float(seed + 1)]
    return [{"name": "x", "shape": [2], "dtype": "float32", "values": values}]


def _hotspot(reference_source_hash: str = SHA_A) -> M1HotspotCorrectnessSpec:
    return M1HotspotCorrectnessSpec(
        hotspot_id="fixture/vector_add",
        reference_implementation="tests.fixture.vector_add",
        reference_source_hash=reference_source_hash,
        cases=(
            M1CorrectnessCase(
                case_id="basic",
                inputs=(M1TensorSpec(name="x", shape=(2,), dtype="float32"),),
                outputs=(
                    M1OutputSpec(
                        name="y",
                        shape=(2,),
                        dtype="float32",
                        atol=0.01,
                        rtol=0.0,
                        equal_nan=True,
                    ),
                ),
                seeds=(0, 1),
                special_values=("ordinary", "nan", "positive_inf"),
                repeats=2,
            ),
        ),
        input_expectations=tuple(
            M1InputExpectation(
                case_id="basic",
                seed=seed,
                special_value=special,
                input_hash=_hash_bytes(canonical_json_bytes(_input_tensors(seed, special))),
            )
            for seed in (0, 1)
            for special in ("ordinary", "nan", "positive_inf")
        ),
    )


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _proc_stat(pid: int, token: int) -> str:
    return f"{pid} (python worker) " + " ".join(["S", *("0" for _ in range(18)), str(token)])


class _Suite:
    def __init__(self, root: Path, *, candidate_delta: float = 0.005) -> None:
        self.root = root
        self.root.mkdir()
        self.protocol = load_registered_m1_protocol()
        reference_source_bytes = b"def vector_add(x): return x + 1\n"
        self.hotspot = _hotspot(_hash_bytes(reference_source_bytes))
        self.task_id = uuid4()
        self.candidate_id = uuid4()
        self.baseline_epoch_id = uuid4()
        self.target_snapshot_id = uuid4()
        self.stage0_run_id = uuid4()
        self.target = load_target(TARGET_PATH)
        self.stage0_protocol = load_registered_stage0_protocol("s0-g0-v2")
        self.stage0_input_digest = "sha256:" + "e" * 64
        self.stage0_mde = write_evidence(
            root / "stage0-mde.json",
            {
                "schema_version": "stage0-formal-report-v1",
                "task_id": str(self.task_id),
                "stage0_run_id": str(self.stage0_run_id),
                "target_snapshot_id": str(self.target_snapshot_id),
                "target_id": self.target.target_id,
                "workload_id": "m1-fixture-v1",
                "adapter_profile": PROFILE,
                "mode": "degraded_manual_intake",
                "automatic_release_allowed": False,
                "verification": {
                    "measurement": "pass",
                    "protocol_version": self.stage0_protocol.protocol.protocol_version,
                    "protocol_hash": self.stage0_protocol.protocol_hash,
                    "input_digest": self.stage0_input_digest,
                    "timer_resolution_ns": 1.0,
                    "noise_sigma_ns": 2.0,
                    "noise_cv": 0.01,
                    "mde_ratio": 0.03,
                },
            },
        )
        self.baseline_snapshot = SourceSnapshot(
            kind="baseline",
            repository="git@example/hcuopt",
            commit="1" * 40,
            tree_hash="2" * 40,
            source_hash="sha256:" + "3" * 64,
            worktree_uri="file:///baseline",
            clean=True,
        )
        self.candidate_snapshot = SourceSnapshot(
            kind="candidate",
            repository="git@example/hcuopt",
            commit="4" * 40,
            tree_hash="5" * 40,
            source_hash="sha256:" + "6" * 64,
            worktree_uri="file:///candidate",
            clean=True,
            parent_snapshot_id=self.baseline_snapshot.snapshot_id,
        )
        artifact_bytes = b"fixture overlay\n"
        self.artifact_hash = _hash_bytes(artifact_bytes)
        self.artifact = ArtifactManifest(
            candidate_id=self.candidate_id,
            kind="python_overlay",
            uri=(root / "artifact.py").as_uri(),
            content_hash=self.artifact_hash,
            source_snapshot_id=self.candidate_snapshot.snapshot_id,
        )
        self.lease_id = uuid4()
        self.context = M1VerificationContext(
            task_id=self.task_id,
            candidate_id=self.candidate_id,
            baseline_epoch_id=self.baseline_epoch_id,
            target_snapshot_id=self.target_snapshot_id,
            target_id=self.target.target_id,
            target_fingerprint=target_fingerprint(self.target),
            workload_id="m1-fixture-v1",
            workload_hash="sha256:" + "8" * 64,
            stage0_run_id=self.stage0_run_id,
            stage0_protocol_hash=self.stage0_protocol.protocol_hash,
            stage0_mde_evidence_uri=self.stage0_mde.uri,
            stage0_mde_evidence_hash=self.stage0_mde.sha256,
            baseline_source_snapshot_id=self.baseline_snapshot.snapshot_id,
            baseline_source_hash=self.baseline_snapshot.source_hash,
            candidate_source_snapshot_id=self.candidate_snapshot.snapshot_id,
            candidate_source_hash=self.candidate_snapshot.source_hash,
            artifact_id=self.artifact.artifact_id,
            artifact_hash=self.artifact.content_hash,
            lease_id=self.lease_id,
            resource_id="hcu-7",
            fencing_token=17,
        )
        baseline_ref = write_evidence(root / "baseline-source.json", self.baseline_snapshot)
        candidate_ref = write_evidence(root / "candidate-source.json", self.candidate_snapshot)
        manifest_ref = write_evidence(root / "artifact-manifest.json", self.artifact)
        artifact_ref = write_evidence_bytes(root / "artifact.py", artifact_bytes)
        reference_source_ref = write_evidence_bytes(root / "reference.py", reference_source_bytes)
        executions = []
        self.output_documents: dict[str, dict[str, object]] = {}
        for ordinal, variant in enumerate(("reference", "candidate"), start=1):
            pid = 1000 + ordinal
            token = 5000 + ordinal
            start = write_evidence(
                root / f"{variant}-start.json",
                {
                    "schema_version": "process-lifecycle-v1",
                    "event": "started",
                    "restart_ordinal": 0,
                    "observer_process_id": 999,
                    "process_id": pid,
                    "proc_stat_line": _proc_stat(pid, token),
                    "captured_monotonic_ns": 100 + ordinal,
                    "waitpid_result_pid": None,
                    "wait_status": None,
                },
            )
            exit_record = write_evidence(
                root / f"{variant}-exit.json",
                {
                    "schema_version": "process-lifecycle-v1",
                    "event": "reaped",
                    "restart_ordinal": 0,
                    "observer_process_id": 999,
                    "process_id": pid,
                    "proc_stat_line": _proc_stat(pid, token),
                    "captured_monotonic_ns": 200 + ordinal,
                    "waitpid_result_pid": pid,
                    "wait_status": 0,
                },
            )
            stdout = write_evidence_bytes(root / f"{variant}.stdout", b"ok\n")
            cache = write_evidence(
                root / f"{variant}-cache.json",
                {
                    "schema_version": "m1-cache-namespace-v1",
                    "variant": variant,
                    "namespace": f"m1-{variant}-{self.candidate_id}",
                    "empty_before_execution": True,
                },
            )
            output_document = self._output_document(variant, candidate_delta)
            self.output_documents[variant] = output_document
            output = write_evidence(
                root / f"{variant}-output.json",
                output_document,
            )
            executions.append(
                {
                    "variant": variant,
                    "process_id": pid,
                    "process_start_token": str(token),
                    "start_record": {"uri": start.uri, "sha256": start.sha256},
                    "exit_record": {"uri": exit_record.uri, "sha256": exit_record.sha256},
                    "stdout": {"uri": stdout.uri, "sha256": stdout.sha256},
                    "normalized_output": {"uri": output.uri, "sha256": output.sha256},
                    "cache_namespace": {"uri": cache.uri, "sha256": cache.sha256},
                }
            )
        binding = {
            **self.context.model_dump(mode="json"),
            "baseline_source_snapshot_id": None,
            "baseline_source_hash": None,
            "candidate_source_snapshot_id": None,
            "candidate_source_hash": None,
            "artifact_id": None,
            "artifact_hash": None,
        }
        for name in [
            "baseline_source_snapshot_id",
            "baseline_source_hash",
            "candidate_source_snapshot_id",
            "candidate_source_hash",
            "artifact_id",
            "artifact_hash",
            "stage0_mde_evidence_uri",
            "stage0_mde_evidence_hash",
        ]:
            binding.pop(name)
        binding.update(
            {
                "protocol_version": self.protocol.protocol.protocol_version,
                "protocol_hash": self.protocol.protocol_hash,
                "hotspot_spec_hash": m1_hotspot_spec_sha256(self.hotspot),
            }
        )
        self.envelope = {
            "schema_version": "m1-kernel-correctness-evidence-v1",
            "binding": binding,
            "baseline_source_snapshot": {"uri": baseline_ref.uri, "sha256": baseline_ref.sha256},
            "candidate_source_snapshot": {"uri": candidate_ref.uri, "sha256": candidate_ref.sha256},
            "reference_source": {
                "uri": reference_source_ref.uri,
                "sha256": reference_source_ref.sha256,
            },
            "artifact_manifest": {"uri": manifest_ref.uri, "sha256": manifest_ref.sha256},
            "artifact": {"uri": artifact_ref.uri, "sha256": artifact_ref.sha256},
            "executions": executions,
            "adapter_provenance": [_provenance("kernel_correctness").model_dump(mode="json")],
            "cleanup_evidence": {
                "fence": {"resource_id": "hcu-7", "fencing_token": 17, "fenced": True},
                "health": {"resource_id": "hcu-7", "healthy": True},
            },
            "producer_summary": {"passed": False, "claim": "ignored"},
        }
        aggregate = write_evidence(root / "correctness-evidence.json", self.envelope)
        self.reference = M1CorrectnessEvidenceReference(uri=aggregate.uri, sha256=aggregate.sha256)

    def with_candidate_output(self, document: dict[str, object]) -> M1CorrectnessEvidenceReference:
        output = write_evidence(self.root / "candidate-output-mutated.json", document)
        envelope = copy.deepcopy(self.envelope)
        candidate = next(item for item in envelope["executions"] if item["variant"] == "candidate")
        candidate["normalized_output"] = {"uri": output.uri, "sha256": output.sha256}
        aggregate = write_evidence(self.root / "correctness-evidence-mutated.json", envelope)
        return M1CorrectnessEvidenceReference(uri=aggregate.uri, sha256=aggregate.sha256)

    def with_envelope(
        self, envelope: dict[str, object], name: str
    ) -> M1CorrectnessEvidenceReference:
        aggregate = write_evidence(self.root / name, envelope)
        return M1CorrectnessEvidenceReference(uri=aggregate.uri, sha256=aggregate.sha256)

    def _output_document(self, variant: str, delta: float) -> dict[str, object]:
        records = []
        for seed in (0, 1):
            for special in ("ordinary", "nan", "positive_inf"):
                for repeat in range(2):
                    values: list[object]
                    if special == "nan":
                        values = ["nan", 2.0]
                    elif special == "positive_inf":
                        values = ["positive_inf", 2.0]
                    else:
                        values = [1.0 + seed, 2.0]
                    if variant == "candidate" and special == "ordinary":
                        values[0] = float(values[0]) + delta
                    records.append(
                        {
                            "case_id": "basic",
                            "seed": seed,
                            "special_value": special,
                            "repeat_ordinal": repeat,
                            "inputs": _input_tensors(seed, special),
                            "outputs": [
                                {
                                    "name": "y",
                                    "shape": [2],
                                    "dtype": "float32",
                                    "values": values,
                                }
                            ],
                        }
                    )
        return {
            "schema_version": "m1-normalized-kernel-output-v1",
            "hotspot_id": self.hotspot.hotspot_id,
            "records": records,
        }

    def verify(self):
        return M1CorrectnessVerifier(
            self.protocol,
            _PortableReader(self.root),
        ).verify(self.context, self.hotspot, self.reference)


def _performance(
    suite: _Suite,
    correctness,
    effects: list[float],
    *,
    summary=None,
    cleanup=True,
    bindings=True,
    baseline_ticks_by_restart: list[int] | None = None,
):
    provenance = _provenance("measurement_harness")
    round_id = uuid4()
    performance_lease_id = uuid4()
    performance_fencing_token = 23
    candidate_ticks = max(1, round(100.0 * (1.0 - fmean(effects))))
    protocol = suite.stage0_protocol.protocol
    expected_samples = (
        protocol.sampling.restart_count
        * len(protocol.sampling.signal_segment_order)
        * protocol.sampling.signal_samples_per_segment
    )
    payload = {
        "task_id": str(suite.context.task_id),
        "candidate_id": str(suite.context.candidate_id),
        "adapter_profile": PROFILE,
        "round_id": str(round_id),
        "baseline_epoch_id": str(suite.context.baseline_epoch_id),
        "stage0_run_id": str(suite.context.stage0_run_id),
        "target_snapshot_id": str(suite.context.target_snapshot_id),
        "target_fingerprint": suite.context.target_fingerprint,
        "target": suite.target.model_dump(mode="json"),
        "workload_id": suite.context.workload_id,
        "workload_hash": suite.context.workload_hash,
        "configuration_hash": "sha256:" + "2" * 64,
        "baseline_source": {"source_hash": suite.context.baseline_source_hash},
        "candidate_source": {"source_hash": suite.context.candidate_source_hash},
        "artifact": suite.artifact.model_dump(mode="json"),
        "stage0_report": {
            "uri": suite.stage0_mde.uri,
            "sha256": suite.stage0_mde.sha256,
            "input_digest": suite.stage0_input_digest,
            "protocol_version": suite.stage0_protocol.protocol.protocol_version,
            "protocol_hash": suite.stage0_protocol.protocol_hash,
        },
        "budget": {"max_samples": expected_samples, "max_wall_seconds": 60},
        "_job_context": {
            "lease_id": str(performance_lease_id),
            "lease_scope": "exclusive",
            "resource_id": suite.context.resource_id,
            "fencing_token": performance_fencing_token,
        },
    }
    clock = FixtureClock()

    def factory(arm, ordinal, _payload, output_dir):
        restart_ordinal = ordinal // 4
        baseline_ticks = (
            baseline_ticks_by_restart[restart_ordinal]
            if baseline_ticks_by_restart is not None
            else 100
        )
        per_restart_candidate_ticks = (
            max(1, round(baseline_ticks * (1.0 - effects[restart_ordinal])))
            if baseline_ticks_by_restart is not None
            else candidate_ticks
        )
        return FixtureWorkload(
            arm,
            ordinal,
            clock,
            suite.target.inference_image.registry_digest,
            suite.artifact.content_hash,
            per_restart_candidate_ticks,
            output_dir,
            provenance,
            baseline_ticks,
        )

    harness = M1TrustedMeasurementHarness(
        provenance=provenance,
        evidence_root=suite.root,
        evidence_reader=_PortableReader(suite.root),
        workload_factory=factory,
        telemetry=FixtureTelemetry(),
        device_timer=FixtureTimer(),
        lifecycle_recorder=FixtureLifecycle(),
        cleaner=FixtureCleaner(),
        clock=clock,
    )
    measurement = harness.run(payload, suite.root)
    original = M1MeasurementEvidence.model_validate_json(
        _PortableReader(suite.root).read_bytes(
            measurement.raw_samples_uri,
            measurement.raw_samples_hash,
        )
    )
    raw_uri = measurement.raw_samples_uri
    raw_hash = measurement.raw_samples_hash
    if not cleanup or not bindings:
        document = original.model_dump(mode="json")
        if not cleanup:
            document["cleanup_evidence"]["health"]["healthy"] = False
        if not bindings:
            document["binding"]["target_fingerprint"] = "sha256:" + "f" * 64
        mutated = write_evidence(
            suite.root / f"performance-{measurement.measurement_id}-mutated.json",
            document,
        )
        raw_uri = mutated.uri
        raw_hash = mutated.sha256
    if summary is not None or raw_hash != measurement.raw_samples_hash:
        measurement = measurement.model_copy(
            update={
                "raw_samples_uri": raw_uri,
                "raw_samples_hash": raw_hash,
                "summary": summary or measurement.summary,
            }
        )
    reference = M1PerformanceEvidenceReference(
        measurement_id=measurement.measurement_id,
        uri=raw_uri,
        sha256=raw_hash,
    )
    report = original.plan.stage0_authority.report
    performance_context = M1PerformanceVerificationContext(
        task_id=suite.context.task_id,
        candidate_id=suite.context.candidate_id,
        round_id=round_id,
        baseline_epoch_id=suite.context.baseline_epoch_id,
        stage0_run_id=suite.context.stage0_run_id,
        target_snapshot_id=suite.context.target_snapshot_id,
        target_id=suite.context.target_id,
        target_fingerprint=suite.context.target_fingerprint,
        environment_fingerprint=original.binding.environment_fingerprint,
        workload_id=suite.context.workload_id,
        workload_hash=suite.context.workload_hash,
        configuration_hash=original.binding.configuration_hash,
        image_digest=original.binding.image_digest,
        baseline_source_hash=suite.context.baseline_source_hash,
        candidate_source_hash=suite.context.candidate_source_hash,
        artifact_id=suite.context.artifact_id,
        artifact_hash=suite.context.artifact_hash,
        adapter_profile=PROFILE,
        stage0_report_uri=report.uri,
        stage0_report_hash=report.sha256,
        stage0_input_digest=report.input_digest,
        stage0_protocol_version=report.protocol_version,
        stage0_protocol_hash=report.protocol_hash,
        lease_id=performance_lease_id,
        resource_id=suite.context.resource_id,
        fencing_token=performance_fencing_token,
    )
    result = M1PerformanceVerifier(
        suite.protocol,
        _PortableReader(suite.root),
    ).verify(
        correctness,
        performance_context,
        reference,
    )
    suite.performance_context = performance_context
    suite.measurement = measurement
    return reference, result


def _correctness_reference_with_outputs(
    suite: _Suite,
    hotspot: M1HotspotCorrectnessSpec,
    reference_document: dict[str, object],
    candidate_document: dict[str, object],
    *,
    name: str,
) -> M1CorrectnessEvidenceReference:
    envelope = copy.deepcopy(suite.envelope)
    envelope["binding"]["hotspot_spec_hash"] = m1_hotspot_spec_sha256(hotspot)
    for variant, document in (
        ("reference", reference_document),
        ("candidate", candidate_document),
    ):
        artifact = write_evidence(suite.root / f"{name}-{variant}.json", document)
        execution = next(item for item in envelope["executions"] if item["variant"] == variant)
        execution["normalized_output"] = {
            "uri": artifact.uri,
            "sha256": artifact.sha256,
        }
    return suite.with_envelope(envelope, f"{name}-evidence.json")


def test_registered_protocol_is_strict_and_hotspot_tolerances_are_explicit(tmp_path: Path) -> None:
    loaded = load_registered_m1_protocol()
    assert loaded.protocol.bootstrap_resamples == 10_000
    assert loaded.protocol_hash.startswith("sha256:")
    with pytest.raises(M1ProtocolError, match="unregistered"):
        load_registered_m1_protocol("m1-loose-v1")
    source = Path(loaded.source_path)
    raw = source.read_text(encoding="utf-8").replace(
        "max_output_elements: 1000000", "max_output_elements: 2"
    )
    modified = tmp_path / "m1-kernel-correctness-v1.yaml"
    modified.write_text(raw, encoding="utf-8")
    altered = load_m1_protocol(modified)
    with pytest.raises(Exception, match="repository-registered"):
        M1CorrectnessVerifier(altered, _PortableReader(tmp_path))
    with pytest.raises(ValidationError, match="atol"):
        M1OutputSpec.model_validate(
            {"name": "y", "shape": [1], "dtype": "float32", "rtol": 0.0, "equal_nan": False}
        )


def test_correctness_verifier_independently_returns_correct(tmp_path: Path) -> None:
    result = _Suite(tmp_path / "evidence").verify()
    assert result.verdict == "correct"
    assert result.checked_records == 12
    assert result.checked_elements == 24
    assert result.mismatch_count == 0


def test_correctness_verifier_distinguishes_incorrect_from_invalid(tmp_path: Path) -> None:
    incorrect = _Suite(tmp_path / "incorrect", candidate_delta=0.25).verify()
    assert incorrect.verdict == "incorrect"
    assert incorrect.failure_codes == ("output_mismatch",)
    suite = _Suite(tmp_path / "invalid")
    invalid_reference = suite.reference.model_copy(update={"sha256": SHA_A})
    invalid = M1CorrectnessVerifier(suite.protocol, _PortableReader(suite.root)).verify(
        suite.context, suite.hotspot, invalid_reference
    )
    assert invalid.verdict == "invalid"
    assert invalid.failure_codes == ("evidence_hash_mismatch",)


@pytest.mark.parametrize(
    ("mutation", "failure_code"),
    [
        (lambda record: record.pop("inputs"), "evidence_schema_invalid"),
        (
            lambda record: record["inputs"][0].update({"shape": [1, 2]}),
            "input_shape_mismatch",
        ),
        (
            lambda record: record["inputs"][0].update({"dtype": "float16"}),
            "input_dtype_mismatch",
        ),
    ],
)
def test_correctness_rejects_missing_or_wrong_input_binding(
    tmp_path: Path, mutation, failure_code: str
) -> None:
    suite = _Suite(tmp_path / failure_code)
    document = copy.deepcopy(suite.output_documents["candidate"])
    mutation(document["records"][0])
    result = M1CorrectnessVerifier(suite.protocol, _PortableReader(suite.root)).verify(
        suite.context,
        suite.hotspot,
        suite.with_candidate_output(document),
    )
    assert result.verdict == "invalid"
    assert result.failure_codes == (failure_code,)


def test_correctness_recomputes_seed_generation_from_frozen_input_hashes(
    tmp_path: Path,
) -> None:
    suite = _Suite(tmp_path / "seed-generation")
    reference = copy.deepcopy(suite.output_documents["reference"])
    candidate = copy.deepcopy(suite.output_documents["candidate"])
    for document in (reference, candidate):
        for record in document["records"]:
            if record["seed"] == 0 and record["special_value"] == "ordinary":
                record["inputs"] = _input_tensors(1, "ordinary")
    result = M1CorrectnessVerifier(suite.protocol, _PortableReader(suite.root)).verify(
        suite.context,
        suite.hotspot,
        _correctness_reference_with_outputs(
            suite,
            suite.hotspot,
            reference,
            candidate,
            name="seed-mismatch",
        ),
    )
    assert result.verdict == "invalid"
    assert result.failure_codes == ("input_generation_mismatch",)


def test_correctness_validates_special_value_semantics_not_only_labels(
    tmp_path: Path,
) -> None:
    suite = _Suite(tmp_path / "special-semantics")
    expectations = tuple(
        item.model_copy(
            update={
                "input_hash": _hash_bytes(
                    canonical_json_bytes(_input_tensors(item.seed, "ordinary"))
                )
            }
        )
        for item in suite.hotspot.input_expectations
    )
    mislabeled_hotspot = suite.hotspot.model_copy(update={"input_expectations": expectations})
    reference = copy.deepcopy(suite.output_documents["reference"])
    candidate = copy.deepcopy(suite.output_documents["candidate"])
    for document in (reference, candidate):
        for record in document["records"]:
            record["inputs"] = _input_tensors(record["seed"], "ordinary")
    result = M1CorrectnessVerifier(suite.protocol, _PortableReader(suite.root)).verify(
        suite.context,
        mislabeled_hotspot,
        _correctness_reference_with_outputs(
            suite,
            mislabeled_hotspot,
            reference,
            candidate,
            name="special-mislabeled",
        ),
    )
    assert result.verdict == "invalid"
    assert result.failure_codes == ("special_value_semantics_mismatch",)


def test_correctness_uses_declared_tolerance_boundary_and_rejects_cross_candidate(
    tmp_path: Path,
) -> None:
    boundary = _Suite(tmp_path / "boundary", candidate_delta=0.009_999).verify()
    assert boundary.verdict == "correct"
    outside = _Suite(tmp_path / "outside", candidate_delta=0.010_001).verify()
    assert outside.verdict == "incorrect"
    suite = _Suite(tmp_path / "cross-candidate")
    other_context = suite.context.model_copy(update={"candidate_id": uuid4()})
    cross_candidate = M1CorrectnessVerifier(suite.protocol, _PortableReader(suite.root)).verify(
        other_context, suite.hotspot, suite.reference
    )
    assert cross_candidate.verdict == "invalid"
    assert cross_candidate.failure_codes == ("immutable_binding_mismatch",)


def test_correctness_rechecks_reference_source_cleanup_and_cache(tmp_path: Path) -> None:
    suite = _Suite(tmp_path / "audited")
    verifier = M1CorrectnessVerifier(suite.protocol, _PortableReader(suite.root))

    changed_source = write_evidence_bytes(suite.root / "reference-changed.py", b"changed\n")
    source_envelope = copy.deepcopy(suite.envelope)
    source_envelope["reference_source"] = {
        "uri": changed_source.uri,
        "sha256": changed_source.sha256,
    }
    source_result = verifier.verify(
        suite.context,
        suite.hotspot,
        suite.with_envelope(source_envelope, "source-mutated.json"),
    )
    assert source_result.failure_codes == ("reference_source_hash_mismatch",)

    cleanup_envelope = copy.deepcopy(suite.envelope)
    cleanup_envelope["cleanup_evidence"]["health"]["resource_id"] = "hcu-6"
    cleanup_result = verifier.verify(
        suite.context,
        suite.hotspot,
        suite.with_envelope(cleanup_envelope, "cleanup-mutated.json"),
    )
    assert cleanup_result.failure_codes == ("cleanup_binding_mismatch",)

    cache_envelope = copy.deepcopy(suite.envelope)
    candidate = next(
        item for item in cache_envelope["executions"] if item["variant"] == "candidate"
    )
    reference = next(
        item for item in cache_envelope["executions"] if item["variant"] == "reference"
    )
    candidate["cache_namespace"] = reference["cache_namespace"]
    cache_result = verifier.verify(
        suite.context,
        suite.hotspot,
        suite.with_envelope(cache_envelope, "cache-mutated.json"),
    )
    assert cache_result.failure_codes == ("cache_binding_mismatch",)

    slow_exit = write_evidence(
        suite.root / "candidate-slow-exit.json",
        {
            "schema_version": "process-lifecycle-v1",
            "event": "reaped",
            "restart_ordinal": 0,
            "observer_process_id": 999,
            "process_id": 1002,
            "proc_stat_line": _proc_stat(1002, 5002),
            "captured_monotonic_ns": 601_000_000_102,
            "waitpid_result_pid": 1002,
            "wait_status": 0,
        },
    )
    slow_envelope = copy.deepcopy(suite.envelope)
    slow_candidate = next(
        item for item in slow_envelope["executions"] if item["variant"] == "candidate"
    )
    slow_candidate["exit_record"] = {"uri": slow_exit.uri, "sha256": slow_exit.sha256}
    slow_result = verifier.verify(
        suite.context,
        suite.hotspot,
        suite.with_envelope(slow_envelope, "slow-mutated.json"),
    )
    assert slow_result.failure_codes == ("execution_budget_exceeded",)

    short_case = suite.hotspot.cases[0].model_copy(update={"repeats": 1})
    short_hotspot = suite.hotspot.model_copy(update={"cases": (short_case,)})
    budget_result = verifier.verify(suite.context, short_hotspot, suite.reference)
    assert budget_result.failure_codes == ("repeat_budget_invalid",)


@pytest.mark.parametrize(
    ("effects", "expected"),
    [
        ([0.12, 0.11, 0.13, 0.12], ManualCandidateVerdict.FASTER),
        ([-0.12, -0.11, -0.13, -0.12], ManualCandidateVerdict.SLOWER),
        ([0.01, -0.01, 0.02, 0.0], ManualCandidateVerdict.INCONCLUSIVE),
    ],
)
def test_performance_verdicts_use_restart_ci_and_stage0_mde(
    tmp_path: Path, effects: list[float], expected: ManualCandidateVerdict
) -> None:
    suite = _Suite(tmp_path / expected.value)
    correctness = suite.verify()
    _, result = _performance(suite, correctness, effects)
    assert result.verdict is expected
    assert result.confidence_interval is not None
    assert result.stage0_mde_ratio == pytest.approx(0.03)
    assert result.workload_mde_ratio == pytest.approx(0.0)
    assert result.credible_threshold == pytest.approx(0.03)


def test_performance_uses_current_workload_mde_when_it_is_more_conservative(
    tmp_path: Path,
) -> None:
    suite = _Suite(tmp_path / "workload-mde")
    correctness = suite.verify()

    _, result = _performance(
        suite,
        correctness,
        [0.15] * 10,
        baseline_ticks_by_restart=[50, 60, 70, 80, 90, 110, 120, 130, 140, 150],
    )

    assert result.verdict is ManualCandidateVerdict.INCONCLUSIVE
    assert result.stage0_mde_ratio == pytest.approx(0.03)
    assert result.workload_mde_ratio is not None
    assert result.workload_mde_ratio > 0.3
    assert result.credible_threshold == result.workload_mde_ratio


def test_performance_ignores_producer_summary_and_fails_closed(tmp_path: Path) -> None:
    suite = _Suite(tmp_path / "valid")
    correctness = suite.verify()
    _, faster = _performance(
        suite,
        correctness,
        [0.1] * 4,
        summary={"verdict": "slower"},
    )
    assert faster.verdict is ManualCandidateVerdict.FASTER
    _, slower = _performance(
        suite,
        correctness,
        [-0.2] * 4,
        summary={"verdict": "faster", "speedup": 99},
    )
    assert slower.verdict is ManualCandidateVerdict.SLOWER
    _, invalid = _performance(suite, correctness, [0.1] * 4, cleanup=False)
    assert invalid.verdict is ManualCandidateVerdict.INVALID
    _, wrong_binding = _performance(suite, correctness, [0.1] * 4, bindings=False)
    assert wrong_binding.verdict is ManualCandidateVerdict.INVALID
    assert wrong_binding.failure_codes == ("performance_binding_mismatch",)
    incorrect_suite = _Suite(tmp_path / "incorrect", candidate_delta=0.25)
    incorrect = incorrect_suite.verify()
    _, blocked = _performance(incorrect_suite, incorrect, [0.1] * 4)
    assert blocked.verdict is ManualCandidateVerdict.INVALID
    assert blocked.effect_ratio is None

    reference, _ = _performance(suite, correctness, [0.1] * 4)
    tampered = reference.model_copy(update={"sha256": SHA_A})
    hash_invalid = M1PerformanceVerifier(
        suite.protocol,
        _PortableReader(suite.root),
    ).verify(correctness, suite.performance_context, tampered)
    assert hash_invalid.verdict is ManualCandidateVerdict.INVALID
    assert hash_invalid.failure_codes == ("evidence_hash_mismatch",)

    wrong_lease = suite.performance_context.model_copy(update={"lease_id": uuid4()})
    lease_invalid = M1PerformanceVerifier(
        suite.protocol,
        _PortableReader(suite.root),
    ).verify(correctness, wrong_lease, reference)
    assert lease_invalid.failure_codes == ("performance_lease_binding_mismatch",)
    wrong_profile = suite.performance_context.model_copy(update={"adapter_profile": "other-real"})
    profile_invalid = M1PerformanceVerifier(
        suite.protocol,
        _PortableReader(suite.root),
    ).verify(correctness, wrong_profile, reference)
    assert profile_invalid.failure_codes == ("performance_binding_mismatch",)

    reader = _PortableReader(suite.root)
    document = reader.read(reference.uri, reference.sha256)
    event_reference = document["acquisitions"][0]["samples"][0]["device_event_record"]
    event = reader.read(event_reference["uri"], event_reference["sha256"])
    event["finished_device_ticks"] += 1
    changed_event = write_evidence(suite.root / "changed-device-event.json", event)
    document["acquisitions"][0]["samples"][0]["device_event_record"] = {
        "uri": changed_event.uri,
        "sha256": changed_event.sha256,
    }
    changed_main = write_evidence(suite.root / "changed-performance-main.json", document)
    changed_reference = reference.model_copy(
        update={"uri": changed_main.uri, "sha256": changed_main.sha256}
    )
    changed_result = M1PerformanceVerifier(suite.protocol, reader).verify(
        correctness,
        suite.performance_context,
        changed_reference,
    )
    assert changed_result.failure_codes == ("performance_event_binding_mismatch",)

    budget_document = reader.read(reference.uri, reference.sha256)
    budget_document["plan"]["stage0_authority"]["sample_budget_hash"] = SHA_A
    changed_budget = write_evidence(suite.root / "changed-sample-budget.json", budget_document)
    budget_result = M1PerformanceVerifier(suite.protocol, reader).verify(
        correctness,
        suite.performance_context,
        reference.model_copy(update={"uri": changed_budget.uri, "sha256": changed_budget.sha256}),
    )
    assert budget_result.verdict is ManualCandidateVerdict.INVALID


@pytest.mark.parametrize("failure_kind", ["correctness", "performance"])
def test_invalid_evidence_still_produces_immutable_failure_bundle(
    tmp_path: Path,
    failure_kind: str,
) -> None:
    suite = _Suite(tmp_path / failure_kind)
    if failure_kind == "correctness":
        correctness = M1CorrectnessVerifier(
            suite.protocol,
            _PortableReader(suite.root),
        ).verify(
            suite.context,
            suite.hotspot,
            suite.reference.model_copy(update={"sha256": SHA_A}),
        )
        performance_reference, performance = _performance(
            suite,
            correctness,
            [0.1] * 4,
        )
    else:
        correctness = suite.verify()
        performance_reference, _ = _performance(suite, correctness, [0.1] * 4)
        performance = M1PerformanceVerifier(
            suite.protocol,
            _PortableReader(suite.root),
        ).verify(
            correctness,
            suite.performance_context,
            performance_reference.model_copy(update={"sha256": SHA_A}),
        )
        performance_reference = performance_reference.model_copy(update={"sha256": SHA_A})

    measurement_provenance = suite.measurement.adapter_provenance
    measurement = suite.measurement.model_copy(
        update={
            "raw_samples_uri": performance_reference.uri,
            "raw_samples_hash": performance_reference.sha256,
            "summary": {"passed": True, "speedup": 999},
        }
    )
    context = M1AdjudicationContext(
        verification=suite.context,
        performance_verification=suite.performance_context,
        job_id=uuid4(),
        round_id=suite.performance_context.round_id,
        measurement=measurement,
        created_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        adapter_provenance=(
            measurement_provenance,
            _provenance("candidate_adjudicator"),
        ),
    )
    result = build_m1_adjudication_result(context, correctness, performance)
    assert isinstance(result, ManualCandidateAdjudicationResult)
    assert result.verdict is ManualCandidateVerdict.INVALID
    assert result.evaluation.passed is None
    assert result.evidence.summary["automatic_release_allowed"] is False
    assert result.evidence.summary["correctness_failure_codes"] == list(correctness.failure_codes)
    detached_measurement = measurement.model_copy(
        update={"raw_samples_hash": "sha256:" + "b" * 64}
    )
    with pytest.raises(ValueError, match="another Performance Reference"):
        build_m1_adjudication_result(
            context.model_copy(update={"measurement": detached_measurement}),
            correctness,
            performance,
        )
    report_root = tmp_path / f"{failure_kind}-report"
    report_root.mkdir()
    first = write_m1_signoff_report(report_root, result, correctness, performance)
    second = write_m1_signoff_report(report_root, result, correctness, performance)
    assert first == second
    assert (report_root / "correctness.json").is_file()
    assert (report_root / "performance.json").is_file()
    assert (report_root / "evidence-bundle.json").is_file()
    assert (report_root / "signoff.md").is_file()
    assert (report_root / "sha256sums.json").is_file()


def test_report_is_deterministic_bound_and_never_authorizes_release(tmp_path: Path) -> None:
    assert M1_SIGNOFF_WARNING == (
        "人工批准仅表示本 Candidate 证据已接受，不授权自动发布、自动安装或生产灰度。"
    )
    suite = _Suite(tmp_path / "evidence")
    correctness = suite.verify()
    _, performance = _performance(suite, correctness, [0.1, 0.11, 0.09, 0.1])
    measurement = suite.measurement.model_copy(
        update={"summary": {"producer_verdict": "ignored"}}
    )
    measurement_provenance = measurement.adapter_provenance
    adjudication_provenance = (
        measurement_provenance,
        _provenance("candidate_adjudicator"),
    )
    context = M1AdjudicationContext(
        verification=suite.context,
        performance_verification=suite.performance_context,
        job_id=uuid4(),
        round_id=suite.performance_context.round_id,
        measurement=measurement,
        created_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        adapter_provenance=adjudication_provenance,
    )
    result = build_m1_adjudication_result(context, correctness, performance)
    wrong_metadata = measurement.model_copy(
        update={
            "metric_name": "throughput",
            "unit": "requests/s",
            "protocol_version": "wrong-v99",
            "sample_count": 1,
            "warmup_count": 0,
            "process_restart_count": 0,
        }
    )
    with pytest.raises(ValueError, match="metadata differs"):
        build_m1_adjudication_result(
            context.model_copy(update={"measurement": wrong_metadata}),
            correctness,
            performance,
        )
    tampered_measurement = measurement.model_copy(
        update={"summary": {"producer_verdict": "faster", "speedup": 999}}
    )
    tampered_context = context.model_copy(update={"measurement": tampered_measurement})
    tampered_result = build_m1_adjudication_result(tampered_context, correctness, performance)
    assert tampered_result.verdict == result.verdict
    assert tampered_result.evidence == result.evidence
    assert result.evidence.summary["automatic_release_allowed"] is False
    assert result.evaluation.passed is True
    report_root = tmp_path / "report"
    report_root.mkdir()
    first = write_m1_signoff_report(report_root, result, correctness, performance)
    second = write_m1_signoff_report(report_root, result, correctness, performance)
    assert first == second
    assert (
        write_m1_signoff_report(
            report_root,
            tampered_result,
            correctness,
            performance,
        )
        == first
    )
    assert M1_SIGNOFF_WARNING in (report_root / "signoff.md").read_text(encoding="utf-8")
    manifest = json.loads((report_root / "sha256sums.json").read_text(encoding="utf-8"))
    for name, item in manifest["files"].items():
        assert _hash_bytes((report_root / name).read_bytes()) == item["sha256"]
    assert "sha256sums.json" not in manifest["files"]
    altered_performance = performance.model_copy(update={"reasons": ("changed",)})
    with pytest.raises(ValueError, match="different content"):
        write_m1_signoff_report(report_root, result, correctness, altered_performance)

    if os.name == "posix":
        linked_root = tmp_path / "linked-report"
        linked_root.symlink_to(report_root, target_is_directory=True)
        with pytest.raises(ValueError, match="existing regular directory"):
            write_m1_signoff_report(linked_root, result, correctness, performance)


@pytest.mark.skipif(os.name != "posix", reason="secure openat reader is POSIX-only")
def test_shared_reader_rejects_symlink_and_hash_tampering(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(canonical_json_bytes({"ok": True}))
    link = allowed / "link.json"
    link.symlink_to(outside)
    reader = HashedEvidenceReader(allowed)
    with pytest.raises(EvidenceReadError) as symlink_error:
        reader.read(link.as_uri(), _hash_bytes(outside.read_bytes()))
    assert symlink_error.value.code == "evidence_symlink"
    file = write_evidence(allowed / "value.json", {"ok": True})
    with pytest.raises(EvidenceReadError) as hash_error:
        reader.read(file.uri, SHA_A)
    assert hash_error.value.code == "evidence_hash_mismatch"


def test_shared_reader_rejects_traversal_and_oversized_evidence(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = write_evidence(tmp_path / "outside.json", {"ok": True})
    reader = _PortableReader(allowed)
    traversal_uri = allowed.as_uri() + "/../outside.json"
    with pytest.raises(EvidenceReadError) as traversal_error:
        reader.read(traversal_uri, outside.sha256)
    assert traversal_error.value.code == "evidence_path_escape"

    oversized = write_evidence_bytes(allowed / "oversized.bin", b"12345")
    bounded = _PortableReader(allowed, max_bytes=4)
    with pytest.raises(EvidenceReadError) as size_error:
        bounded.read_raw_bytes(oversized.uri, oversized.sha256)
    assert size_error.value.code == "evidence_too_large"
