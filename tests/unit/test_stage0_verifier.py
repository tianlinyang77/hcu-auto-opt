from __future__ import annotations

import hashlib
import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from hcuopt.contracts.platform_v1 import AdapterProvenance, TargetSpec
from hcuopt.domain.enums import (
    GateResult,
    HotPatchCapability,
    LeaseScope,
    ProfilerCapability,
    Stage0ProbeType,
    Stage0RunMode,
)
from hcuopt.evaluation.stage0_protocol import (
    LoadedStage0Protocol,
    load_registered_stage0_protocol,
)
from hcuopt.evaluation.stage0_verifier import (
    FingerprintEvidenceV2,
    HotpatchEvidenceV2,
    ProfilerEvidenceV2,
    Stage0EvidenceError,
    Stage0EvidenceReader,
    Stage0ProbeEvidenceReference,
    Stage0VerificationContext,
    Stage0Verifier,
    classify_hotpatch,
    classify_profiler,
    target_fingerprint,
    verification_input_digest,
)
from hcuopt.measurement.evidence import canonical_json_bytes, write_evidence

requires_posix_reader = pytest.mark.skipif(
    os.name != "posix",
    reason="Formal evidence verification requires POSIX openat/O_NOFOLLOW semantics",
)

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_ROOT = ROOT / "config" / "stage0"
TARGET_PATH = ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml"

SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64
TASK_ID = UUID(int=101)
RUN_ID = UUID(int=102)
TARGET_SNAPSHOT_ID = UUID(int=103)
LEASE_ID = UUID(int=104)
RESOURCE_ID = "hcu-7"
FENCING_TOKEN = 17
WORKLOAD_ID = "stage0-short-kernel-v1"
PROFILE = "nmz36-stage0-composite-v1"

PROVENANCE = AdapterProvenance(
    profile=PROFILE,
    capability="stage0_probe",
    adapter_name="CompositeStage0Adapter",
    adapter_version="2",
    implementation_kind="real",
    source_commit="1" * 40,
)
HEALTHY_CLEANUP = {
    "fence": {
        "fenced": True,
        "resource_id": RESOURCE_ID,
        "fencing_token": FENCING_TOKEN,
    },
    "health": {
        "healthy": True,
        "resource_id": RESOURCE_ID,
        "remaining_processes": [],
    },
}


class _PortableVerifierTestReader(Stage0EvidenceReader):
    """Exercise pure verifier logic on Windows without claiming Formal path safety."""

    def _secure_read(self, path: Path) -> bytes:
        encoded = path.read_bytes()
        if len(encoded) > self.max_bytes:
            raise Stage0EvidenceError("evidence_too_large", "test evidence exceeds size limit")
        return encoded


def _verifier_reader(root: Path) -> Stage0EvidenceReader:
    if os.name == "posix":
        return Stage0EvidenceReader(root)
    return _PortableVerifierTestReader(root)


def _load_target() -> TargetSpec:
    return TargetSpec.model_validate(yaml.safe_load(TARGET_PATH.read_text(encoding="utf-8")))


def _hash_bytes(encoded: bytes) -> str:
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _telemetry(
    *,
    temperature_c: float = 55.0,
    sclk_mhz: float = 1350.0,
    mclk_mhz: float = 875.0,
    performance_level: str = "manual",
    cache_state: str = "flushed",
    unmanaged_accelerator_process: bool = False,
) -> dict[str, Any]:
    background_processes: list[dict[str, Any]] = []
    if unmanaged_accelerator_process:
        background_processes.append(
            {
                "process_id": 9999,
                "executable": "/usr/bin/noisy-neighbor",
                "command_line": "noisy-neighbor --device 7",
                "uses_accelerator": True,
                "device_memory_bytes": 4096,
                "managed_by_stage0": False,
            }
        )
    return {
        "device": {
            "device_index": 7,
            "temperature_c": temperature_c,
            "hotspot_temperature_c": temperature_c,
            "sclk_mhz": sclk_mhz,
            "mclk_mhz": mclk_mhz,
            "performance_level": performance_level,
            "power_w": 210.0,
        },
        "cache": {
            "state": cache_state,
            "cleared_before_sample": True,
            "identity_hash": None,
        },
        "background_processes": background_processes,
    }


def _binding(
    probe_type: Stage0ProbeType,
    protocol: LoadedStage0Protocol,
    target: TargetSpec,
) -> dict[str, Any]:
    return {
        "task_id": str(TASK_ID),
        "stage0_run_id": str(RUN_ID),
        "target_snapshot_id": str(TARGET_SNAPSHOT_ID),
        "target_id": target.target_id,
        "target_fingerprint": target_fingerprint(target),
        "environment_fingerprint": SHA_B,
        "workload_id": WORKLOAD_ID,
        "probe_type": probe_type.value,
        "run_mode": Stage0RunMode.FORMAL.value,
        "protocol_version": protocol.protocol.protocol_version,
        "protocol_hash": protocol.protocol_hash,
        "metric_name": "kernel_elapsed",
        "unit": "ns",
        "measurement_id": str(UUID(int=200 + list(Stage0ProbeType).index(probe_type))),
        "lease": {
            "lease_id": str(LEASE_ID),
            "lease_scope": LeaseScope.EXCLUSIVE.value,
            "resource_id": RESOURCE_ID,
            "fencing_token": FENCING_TOKEN,
        },
    }


def _calibration(*, timer_resolution_ns: float, max_residual_ns: float) -> dict[str, Any]:
    interval_ns = 2
    resolution_tick_delta = max(1, int(round(timer_resolution_ns)))
    middle_offset_ns = int(round(max_residual_ns * 1.5))
    midpoints = (10_001, 11_001 + middle_offset_ns, 12_001)
    points = []
    for ordinal, (device_ticks, midpoint) in enumerate(
        zip((1_000, 2_000, 3_000), midpoints, strict=True)
    ):
        started = midpoint - interval_ns // 2
        finished = started + interval_ns
        points.append(
            {
                "point_ordinal": ordinal,
                "device_ticks": device_ticks,
                "host_started_monotonic_ns": started,
                "host_finished_monotonic_ns": finished,
            }
        )
    actual_resolution = float(resolution_tick_delta)
    actual_residual = middle_offset_ns * (2.0 / 3.0) + interval_ns / 2.0
    return {
        "device_name": "hcu-event",
        "device_index": 7,
        "timer_resolution_ns": actual_resolution,
        "ns_per_tick": 1.0,
        "max_residual_ns": actual_residual,
        "points": points,
        "resolution_tick_deltas": [resolution_tick_delta] * 3,
    }


def _samples(
    *,
    segment_order: tuple[str, ...],
    samples_per_segment: int,
    restart_values: tuple[float, ...] | None = None,
    comparison_effect: float = 0.0,
    outlier_count: int = 0,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    acquisition = 0
    for restart in range(10):
        baseline_ns = 1_000.0 if restart_values is None else restart_values[restart]
        for segment in segment_order:
            arm = "single"
            nominal_sample_ns = baseline_ns
            if segment in {"A1", "A2"}:
                arm = "baseline"
            elif segment in {"B1", "B2"}:
                arm = "comparison"
                nominal_sample_ns = baseline_ns * (1.0 + comparison_effect)
            for segment_ordinal in range(samples_per_segment):
                sample_ns = (
                    baseline_ns * 2.0
                    if acquisition < outlier_count
                    else nominal_sample_ns
                )
                started_device_ticks = 1_000_000 + acquisition * 200_000
                elapsed_ticks = int(round(sample_ns * 100))
                started_host_ns = 2_000_000 + acquisition * 200_000
                result.append(
                    {
                        "process_id": 10_000 + restart,
                        "restart_ordinal": restart,
                        "arm": arm,
                        "segment": segment,
                        "acquisition_ordinal": acquisition,
                        "segment_sample_ordinal": segment_ordinal,
                        "started_monotonic_ns": started_host_ns,
                        "finished_monotonic_ns": started_host_ns + elapsed_ticks,
                        "started_device_ticks": started_device_ticks,
                        "finished_device_ticks": started_device_ticks + elapsed_ticks,
                        "batch_iterations": 100,
                    }
                )
                acquisition += 1
    return result


def _measurement_evidence(
    probe_type: Stage0ProbeType,
    protocol: LoadedStage0Protocol,
    target: TargetSpec,
    *,
    restart_values: tuple[float, ...] | None = None,
    comparison_effect: float = 0.0,
    timer_resolution_ns: float = 1.0,
    max_residual_ns: float = 0.5,
    before_telemetry: dict[str, Any] | None = None,
    after_telemetry: dict[str, Any] | None = None,
    outlier_count: int = 0,
) -> dict[str, Any]:
    if probe_type is Stage0ProbeType.TIMER:
        segment_order = ("timer",)
        samples_per_segment = 1
    elif probe_type is Stage0ProbeType.NOISE:
        segment_order = ("noise",)
        samples_per_segment = 30
    else:
        segment_order = ("A1", "B1", "B2", "A2")
        samples_per_segment = 10
    return {
        "schema_version": "measurement-evidence-v2",
        "binding": _binding(probe_type, protocol, target),
        "plan": {
            "restart_count": 10,
            "warmup_count": 10,
            "batch_iterations": 100,
            "segment_order": list(segment_order),
            "samples_per_segment": samples_per_segment,
        },
        "calibration": _calibration(
            timer_resolution_ns=timer_resolution_ns,
            max_residual_ns=max_residual_ns,
        ),
        "samples": _samples(
            segment_order=segment_order,
            samples_per_segment=samples_per_segment,
            restart_values=restart_values,
            comparison_effect=comparison_effect,
            outlier_count=outlier_count,
        ),
        "observations": [
            {
                "phase": "before_run",
                "captured_monotonic_ns": 1_000_000,
                "restart_ordinal": None,
                "acquisition_ordinal": None,
                "telemetry": before_telemetry or _telemetry(),
            },
            {
                "phase": "after_run",
                "captured_monotonic_ns": 1_000_000_000,
                "restart_ordinal": None,
                "acquisition_ordinal": None,
                "telemetry": after_telemetry or _telemetry(),
            },
        ],
        "adapter_provenance": [PROVENANCE.model_dump(mode="json")],
        "synthetic": False,
    }


def _raw_suite(
    protocol: LoadedStage0Protocol,
    target: TargetSpec,
    *,
    noise_restart_values: tuple[float, ...] | None = None,
    known_effect: float = 0.12,
    null_effect: float = 0.0,
    timer_resolution_ns: float = 1.0,
    max_residual_ns: float = 0.5,
    before_telemetry: dict[str, Any] | None = None,
    after_telemetry: dict[str, Any] | None = None,
    noise_outlier_count: int = 0,
) -> dict[Stage0ProbeType, dict[str, Any]]:
    common = {
        "schema_version": "measurement-evidence-v2",
        "observations": [
            {
                "phase": "before_run",
                "captured_monotonic_ns": 1_000_000,
                "restart_ordinal": None,
                "acquisition_ordinal": None,
                "telemetry": before_telemetry or _telemetry(),
            },
            {
                "phase": "after_run",
                "captured_monotonic_ns": 1_000_000_000,
                "restart_ordinal": None,
                "acquisition_ordinal": None,
                "telemetry": after_telemetry or _telemetry(),
            },
        ],
        "adapter_provenance": [PROVENANCE.model_dump(mode="json")],
        "synthetic": False,
    }
    return {
        Stage0ProbeType.FINGERPRINT: {
            **common,
            "binding": _binding(Stage0ProbeType.FINGERPRINT, protocol, target),
            "hardware_fingerprint": SHA_C,
            "software_fingerprint": SHA_D,
        },
        Stage0ProbeType.TIMER: _measurement_evidence(
            Stage0ProbeType.TIMER,
            protocol,
            target,
            timer_resolution_ns=timer_resolution_ns,
            max_residual_ns=max_residual_ns,
            before_telemetry=before_telemetry,
            after_telemetry=after_telemetry,
        ),
        Stage0ProbeType.NOISE: _measurement_evidence(
            Stage0ProbeType.NOISE,
            protocol,
            target,
            restart_values=noise_restart_values,
            timer_resolution_ns=timer_resolution_ns,
            max_residual_ns=max_residual_ns,
            before_telemetry=before_telemetry,
            after_telemetry=after_telemetry,
            outlier_count=noise_outlier_count,
        ),
        Stage0ProbeType.KNOWN_SIGNAL: _measurement_evidence(
            Stage0ProbeType.KNOWN_SIGNAL,
            protocol,
            target,
            comparison_effect=known_effect,
            timer_resolution_ns=timer_resolution_ns,
            max_residual_ns=max_residual_ns,
            before_telemetry=before_telemetry,
            after_telemetry=after_telemetry,
        ),
        Stage0ProbeType.NULL_SIGNAL: _measurement_evidence(
            Stage0ProbeType.NULL_SIGNAL,
            protocol,
            target,
            comparison_effect=null_effect,
            timer_resolution_ns=timer_resolution_ns,
            max_residual_ns=max_residual_ns,
            before_telemetry=before_telemetry,
            after_telemetry=after_telemetry,
        ),
        Stage0ProbeType.PROFILER: {
            **common,
            "binding": _binding(Stage0ProbeType.PROFILER, protocol, target),
            "parser_succeeded": True,
            "kernels": [
                {
                    "kernel_name": "hgemm_128x128",
                    "duration_ns": 900.0,
                    "call_count": 10,
                    "shapes": ["1x128x128"],
                    "dtypes": ["float16"],
                    "metadata": {"grid": "128x1x1"},
                    "python_source": "model.layers.0.mlp",
                    "hip_symbol": "_Z17hgemm_128x128v",
                }
            ],
        },
        Stage0ProbeType.HOTPATCH: {
            **common,
            "binding": _binding(Stage0ProbeType.HOTPATCH, protocol, target),
            "activation_mode": "runtime_hot_patch",
            "original_state_hash": SHA_A,
            "activated_state_hash": SHA_B,
            "recovered_state_hash": SHA_A,
            "baseline_output_hash": SHA_C,
            "activated_output_hash": SHA_C,
            "baseline_cache_namespace_hash": SHA_C,
            "activated_cache_namespace_hash": SHA_D,
        },
    }


@dataclass
class _Suite:
    root: Path
    protocol: LoadedStage0Protocol
    context: Stage0VerificationContext
    raw: dict[Stage0ProbeType, dict[str, Any]]
    paths: dict[Stage0ProbeType, Path]
    references: dict[Stage0ProbeType, Stage0ProbeEvidenceReference]

    def rewrite(self, probe_type: Stage0ProbeType) -> None:
        # Production evidence is write-once. These negative tests intentionally
        # replace a temporary fixture before verification to model a producer
        # that published malformed or incorrectly bound raw evidence.
        self.paths[probe_type].unlink()
        artifact = write_evidence(self.paths[probe_type], self.raw[probe_type])
        self.references[probe_type] = self.references[probe_type].model_copy(
            update={"raw_evidence_hash": artifact.sha256}
        )

    def verify(self):
        return Stage0Verifier(self.protocol, _verifier_reader(self.root)).verify(
            self.context,
            tuple(self.references.values()),
        )


def _build_suite(tmp_path: Path, **raw_options: Any) -> _Suite:
    protocol = load_registered_stage0_protocol("s0-g0-v1", config_root=PROTOCOL_ROOT)
    target = _load_target()
    context = Stage0VerificationContext(
        task_id=TASK_ID,
        stage0_run_id=RUN_ID,
        target_snapshot_id=TARGET_SNAPSHOT_ID,
        target=target,
        target_fingerprint=target_fingerprint(target),
        workload_id=WORKLOAD_ID,
        adapter_profile=PROFILE,
        expected_resource_id=RESOURCE_ID,
    )
    evidence_root = tmp_path / "results" / "stage0" / str(RUN_ID)
    evidence_root.mkdir(parents=True)
    raw = _raw_suite(protocol, target, **raw_options)
    paths: dict[Stage0ProbeType, Path] = {}
    references: dict[Stage0ProbeType, Stage0ProbeEvidenceReference] = {}
    for probe_type in Stage0ProbeType:
        path = evidence_root / probe_type.value / "raw.json"
        artifact = write_evidence(path, raw[probe_type])
        paths[probe_type] = path
        references[probe_type] = Stage0ProbeEvidenceReference(
            probe_record_id=UUID(int=300 + list(Stage0ProbeType).index(probe_type)),
            probe_type=probe_type,
            raw_evidence_uri=artifact.uri,
            raw_evidence_hash=artifact.sha256,
            adapter_provenance=(PROVENANCE,),
            lease_id=LEASE_ID,
            resource_id=RESOURCE_ID,
            fencing_token=FENCING_TOKEN,
            cleanup_evidence=deepcopy(HEALTHY_CLEANUP),
        )
    return _Suite(
        root=evidence_root,
        protocol=protocol,
        context=context,
        raw=raw,
        paths=paths,
        references=references,
    )


def _parse_profiler(raw: dict[str, Any]) -> ProfilerEvidenceV2:
    return ProfilerEvidenceV2.model_validate_json(canonical_json_bytes(raw))


def _parse_hotpatch(raw: dict[str, Any]) -> HotpatchEvidenceV2:
    return HotpatchEvidenceV2.model_validate_json(canonical_json_bytes(raw))


@requires_posix_reader
def test_safe_reader_accepts_only_canonical_content_with_matching_hash(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    path = allowed / "raw.json"
    encoded = canonical_json_bytes({"probe": "noise", "value": 1})
    path.write_bytes(encoded)
    reader = Stage0EvidenceReader(allowed)

    assert reader.read(path.as_uri(), _hash_bytes(encoded)) == {"probe": "noise", "value": 1}

    with pytest.raises(Stage0EvidenceError) as caught:
        reader.read(path.as_uri(), SHA_A)
    assert caught.value.code == "evidence_hash_mismatch"


@requires_posix_reader
def test_safe_reader_rejects_path_escape_noncanonical_and_oversized_files(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.json"
    outside_bytes = canonical_json_bytes({"outside": True})
    outside.write_bytes(outside_bytes)
    reader = Stage0EvidenceReader(allowed)

    with pytest.raises(Stage0EvidenceError) as caught:
        reader.read(outside.as_uri(), _hash_bytes(outside_bytes))
    assert caught.value.code == "evidence_path_escape"

    noncanonical = allowed / "pretty.json"
    pretty_bytes = b'{\n  "value": 1\n}\n'
    noncanonical.write_bytes(pretty_bytes)
    with pytest.raises(Stage0EvidenceError) as caught:
        reader.read(noncanonical.as_uri(), _hash_bytes(pretty_bytes))
    assert caught.value.code == "evidence_noncanonical"

    oversized = allowed / "oversized.json"
    oversized_bytes = canonical_json_bytes({"value": "too-large"})
    oversized.write_bytes(oversized_bytes)
    with pytest.raises(Stage0EvidenceError) as caught:
        Stage0EvidenceReader(allowed, max_bytes=8).read(
            oversized.as_uri(), _hash_bytes(oversized_bytes)
        )
    assert caught.value.code == "evidence_too_large"


@requires_posix_reader
def test_safe_reader_rejects_symbolic_links_even_when_target_stays_inside_root(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    target = allowed / "target.json"
    encoded = canonical_json_bytes({"value": 1})
    target.write_bytes(encoded)
    link = allowed / "linked.json"
    try:
        link.symlink_to(target)
    except OSError as exc:  # pragma: no cover - depends on Windows developer-mode policy.
        pytest.skip(f"symbolic links are unavailable: {exc}")

    with pytest.raises(Stage0EvidenceError) as caught:
        Stage0EvidenceReader(allowed).read(link.as_uri(), _hash_bytes(encoded))
    assert caught.value.code == "evidence_symlink"


@requires_posix_reader
def test_safe_reader_rejects_non_regular_files_and_non_finite_json(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    directory = allowed / "directory"
    directory.mkdir()
    reader = Stage0EvidenceReader(allowed)

    with pytest.raises(Stage0EvidenceError) as caught:
        reader.read(directory.as_uri(), SHA_A)
    assert caught.value.code == "evidence_not_regular"

    invalid = allowed / "non-finite.json"
    encoded = b'{"value":NaN}\n'
    invalid.write_bytes(encoded)
    with pytest.raises(Stage0EvidenceError) as caught:
        reader.read(invalid.as_uri(), _hash_bytes(encoded))
    assert caught.value.code == "evidence_invalid_json"

    nested: dict[str, Any] = {"leaf": True}
    for _ in range(70):
        nested = {"nested": nested}
    deeply_nested = allowed / "deep.json"
    deep_bytes = canonical_json_bytes(nested)
    deeply_nested.write_bytes(deep_bytes)
    with pytest.raises(Stage0EvidenceError) as caught:
        reader.read(deeply_nested.as_uri(), _hash_bytes(deep_bytes))
    assert caught.value.code == "evidence_invalid_json"


@requires_posix_reader
def test_safe_reader_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    fifo = allowed / "raw.fifo"
    os.mkfifo(fifo)

    with pytest.raises(Stage0EvidenceError) as caught:
        Stage0EvidenceReader(allowed).read(fifo.as_uri(), SHA_A)

    assert caught.value.code == "evidence_not_regular"


@pytest.mark.skipif(os.name == "posix", reason="non-POSIX fail-closed behavior")
def test_formal_reader_fails_closed_without_posix_nofollow(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    path = allowed / "raw.json"
    encoded = canonical_json_bytes({"value": 1})
    path.write_bytes(encoded)

    with pytest.raises(Stage0EvidenceError) as caught:
        Stage0EvidenceReader(allowed).read(path.as_uri(), _hash_bytes(encoded))

    assert caught.value.code == "evidence_platform_unsupported"


def test_seven_probe_canonical_evidence_is_recomputed_to_full_pass(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)

    result = suite.verify()

    assert result.measurement is GateResult.PASS
    assert result.profiler is ProfilerCapability.FULL
    assert result.hot_patch is HotPatchCapability.HOT_PATCH
    assert result.failure_codes == ()
    assert result.noise_cv == 0.0
    assert result.mde_ratio == 0.0
    assert result.statistics["known_signal"]["effect_ratio"] == pytest.approx(0.12)
    assert result.statistics["null_signal"]["effect_ratio"] == 0.0
    assert len(result.input_evidence) == 7
    assert result.verifier_provenance.implementation_kind == "real"


def test_formal_verifier_rejects_modified_thresholds_with_a_registered_name(
    tmp_path: Path,
) -> None:
    suite = _build_suite(tmp_path / "suite")
    custom_root = tmp_path / "custom-protocol"
    custom_root.mkdir()
    raw = yaml.safe_load((PROTOCOL_ROOT / "s0-g0-v1.yaml").read_text(encoding="utf-8"))
    raw["measurement_gates"]["max_cv_ratio"] = 0.50
    (custom_root / "s0-g0-v1.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False),
        encoding="utf-8",
    )
    modified = load_registered_stage0_protocol("s0-g0-v1", config_root=custom_root)

    with pytest.raises(Stage0EvidenceError) as caught:
        Stage0Verifier(modified, Stage0EvidenceReader(suite.root))

    assert caught.value.code == "protocol_not_registered"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("timer_resolution_ns", 999.0),
        ("ns_per_tick", 2.0),
        ("max_residual_ns", 999.0),
    ],
)
def test_claimed_calibration_summary_cannot_override_raw_points(
    tmp_path: Path,
    field: str,
    replacement: float,
) -> None:
    suite = _build_suite(tmp_path)
    suite.raw[Stage0ProbeType.NOISE]["calibration"][field] = replacement
    suite.rewrite(Stage0ProbeType.NOISE)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()

    assert caught.value.code == "calibration_summary_mismatch"


def test_input_digest_reference_surface_excludes_untrusted_database_summary(
    tmp_path: Path,
) -> None:
    suite = _build_suite(tmp_path)
    original_records = [
        {
            **reference.model_dump(mode="python"),
            "summary": {"cv": 0.001, "known_signal_detected": True},
        }
        for reference in suite.references.values()
    ]
    tampered_records = deepcopy(original_records)
    for record in tampered_records:
        record["summary"] = {
            "cv": 999.0,
            "mde": 999.0,
            "known_signal_detected": False,
            "capability": "none",
        }

    assert "summary" not in Stage0ProbeEvidenceReference.model_fields
    with pytest.raises(ValidationError, match="summary"):
        Stage0ProbeEvidenceReference.model_validate(original_records[0])

    def project(records: list[dict[str, Any]]):
        fields = Stage0ProbeEvidenceReference.model_fields
        return {
            record["probe_type"]: Stage0ProbeEvidenceReference.model_validate(
                {name: value for name, value in record.items() if name in fields}
            )
            for record in records
        }

    before = verification_input_digest(suite.context, project(original_records), suite.protocol)
    after = verification_input_digest(suite.context, project(tampered_records), suite.protocol)
    assert before == after


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("task_id", str(UUID(int=999))),
        ("target_fingerprint", SHA_A),
        ("workload_id", "different-workload"),
        ("protocol_hash", SHA_A),
    ],
)
def test_raw_identity_binding_mismatch_is_rejected(
    tmp_path: Path,
    field: str,
    replacement: Any,
) -> None:
    suite = _build_suite(tmp_path)
    suite.raw[Stage0ProbeType.NOISE]["binding"][field] = replacement
    suite.rewrite(Stage0ProbeType.NOISE)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()
    assert caught.value.code == "evidence_binding_mismatch"


def test_context_target_mutation_is_detected_before_verification(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    suite.context.target.execution_host.accelerator.expected_sclk_mhz = 999

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()

    assert caught.value.code == "target_snapshot_mutated"

    with pytest.raises(Stage0EvidenceError) as digest_error:
        verification_input_digest(suite.context, suite.references, suite.protocol)
    assert digest_error.value.code == "target_snapshot_mutated"


def test_mutated_cleanup_reference_is_revalidated_before_use(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    reference = suite.references[Stage0ProbeType.NOISE]
    reference.cleanup_evidence["health"]["healthy"] = False

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()

    assert caught.value.code == "reference_schema_invalid"


def test_huge_raw_integer_is_classified_as_invalid_evidence(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    suite.raw[Stage0ProbeType.NOISE]["calibration"]["points"][2]["device_ticks"] = 10**1000
    suite.rewrite(Stage0ProbeType.NOISE)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()

    assert caught.value.code == "evidence_schema_invalid"


def test_all_timing_probes_cannot_relabel_the_registered_metric(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    for probe_type in (
        Stage0ProbeType.TIMER,
        Stage0ProbeType.NOISE,
        Stage0ProbeType.KNOWN_SIGNAL,
        Stage0ProbeType.NULL_SIGNAL,
    ):
        suite.raw[probe_type]["binding"]["metric_name"] = "throughput"
        suite.raw[probe_type]["binding"]["unit"] = "us"
        suite.rewrite(probe_type)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()

    assert caught.value.code == "metric_binding_mismatch"


@pytest.mark.parametrize(
    ("lease_field", "replacement"),
    [
        ("lease_id", str(UUID(int=999))),
        ("resource_id", "hcu-6"),
        ("fencing_token", 18),
    ],
)
def test_raw_lease_and_fencing_binding_mismatch_is_rejected(
    tmp_path: Path,
    lease_field: str,
    replacement: Any,
) -> None:
    suite = _build_suite(tmp_path)
    suite.raw[Stage0ProbeType.TIMER]["binding"]["lease"][lease_field] = replacement
    suite.rewrite(Stage0ProbeType.TIMER)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()
    assert caught.value.code == "evidence_binding_mismatch"


def test_telemetry_and_clock_calibration_must_match_target_device(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    noise = suite.raw[Stage0ProbeType.NOISE]
    noise["calibration"]["device_index"] = 0
    for observation in noise["observations"]:
        observation["telemetry"]["device"]["device_index"] = 0
    suite.rewrite(Stage0ProbeType.NOISE)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()

    assert caught.value.code == "evidence_device_mismatch"


def test_seven_probe_barrier_cannot_mix_accelerator_resources(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    reference = suite.references[Stage0ProbeType.NOISE]
    payload = reference.model_dump(mode="python")
    payload["resource_id"] = "hcu-6"
    payload["cleanup_evidence"] = {
        "fence": {"fenced": True, "resource_id": "hcu-6", "fencing_token": FENCING_TOKEN},
        "health": {"healthy": True, "resource_id": "hcu-6", "remaining_processes": []},
    }
    suite.references[Stage0ProbeType.NOISE] = Stage0ProbeEvidenceReference.model_validate(
        payload
    )

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()

    assert caught.value.code == "resource_binding_mismatch"


def test_shared_formal_lease_is_schema_invalid(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    suite.raw[Stage0ProbeType.NOISE]["binding"]["lease"]["lease_scope"] = "shared"
    suite.rewrite(Stage0ProbeType.NOISE)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()
    assert caught.value.code == "evidence_schema_invalid"


def test_each_probe_requires_a_distinct_measurement_id(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    reused = suite.raw[Stage0ProbeType.TIMER]["binding"]["measurement_id"]
    suite.raw[Stage0ProbeType.NOISE]["binding"]["measurement_id"] = reused
    suite.rewrite(Stage0ProbeType.NOISE)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()
    assert caught.value.code == "measurement_id_reused"


def test_all_timing_probes_require_one_metric_and_unit_binding(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    suite.raw[Stage0ProbeType.NULL_SIGNAL]["binding"]["unit"] = "us"
    suite.rewrite(Stage0ProbeType.NULL_SIGNAL)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()
    assert caught.value.code == "metric_binding_mismatch"


def test_raw_and_recorded_provenance_must_match(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    suite.raw[Stage0ProbeType.PROFILER]["adapter_provenance"][0][
        "adapter_name"
    ] = "DifferentRealAdapter"
    suite.rewrite(Stage0ProbeType.PROFILER)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()
    assert caught.value.code == "provenance_mismatch"


@pytest.mark.parametrize(
    "invalid_values",
    [
        {"adapter_provenance": (PROVENANCE.model_copy(update={"implementation_kind": "fake"}),)},
        {
            "cleanup_evidence": {
                "fence": {"fenced": True},
                "health": {"healthy": False},
            }
        },
    ],
)
def test_reference_rejects_fake_provenance_or_unhealthy_cleanup(
    tmp_path: Path,
    invalid_values: dict[str, Any],
) -> None:
    suite = _build_suite(tmp_path)
    payload = suite.references[Stage0ProbeType.NOISE].model_dump(mode="python")
    payload.update(invalid_values)

    with pytest.raises(ValidationError):
        Stage0ProbeEvidenceReference.model_validate(payload)


def test_cleanup_evidence_is_bound_to_reference_resource_and_fencing_token(
    tmp_path: Path,
) -> None:
    suite = _build_suite(tmp_path)
    payload = suite.references[Stage0ProbeType.NOISE].model_dump(mode="python")
    payload["cleanup_evidence"] = {
        "fence": {
            "fenced": True,
            "resource_id": "hcu-6",
            "fencing_token": FENCING_TOKEN + 1,
        },
        "health": {"healthy": True, "resource_id": "hcu-6"},
    }

    with pytest.raises(ValidationError, match="cleanup"):
        Stage0ProbeEvidenceReference.model_validate(payload)


@pytest.mark.parametrize(
    ("case", "options", "expected_codes"),
    [
        (
            "noise",
            {"noise_restart_values": (900.0, 1100.0) * 5},
            {"noise_cv_exceeded", "noise_mde_exceeded"},
        ),
        (
            "known",
            {"known_effect": 0.02},
            {"known_signal_not_detected"},
        ),
        (
            "null",
            {"null_effect": 0.04},
            {"null_signal_not_equivalent"},
        ),
        (
            "timer_environment",
            {
                "timer_resolution_ns": 20.0,
                "max_residual_ns": 20.0,
                "before_telemetry": _telemetry(
                    temperature_c=70.0,
                    sclk_mhz=1200.0,
                    mclk_mhz=800.0,
                    performance_level="auto",
                    cache_state="unknown",
                    unmanaged_accelerator_process=True,
                ),
                "after_telemetry": _telemetry(
                    temperature_c=80.0,
                    sclk_mhz=1200.0,
                    mclk_mhz=800.0,
                    performance_level="auto",
                    cache_state="unknown",
                    unmanaged_accelerator_process=True,
                ),
            },
            {
                "timer_timer_resolution_exceeded",
                "timer_calibration_residual_exceeded",
                "temperature_drift_exceeded",
                "sclk_drift_exceeded",
                "mclk_drift_exceeded",
                "performance_level_mismatch",
                "cache_state_unknown",
                "background_accelerator_process",
            },
        ),
    ],
)
def test_four_measurement_gate_classes_fail_from_recomputed_raw_values(
    tmp_path: Path,
    case: str,
    options: dict[str, Any],
    expected_codes: set[str],
) -> None:
    suite = _build_suite(tmp_path / case, **options)

    result = suite.verify()

    assert result.measurement is GateResult.FAIL
    assert expected_codes.issubset(result.failure_codes)


def test_hampel_outliers_are_retained_but_excess_ratio_fails_gate(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path, noise_outlier_count=20)

    result = suite.verify()

    assert result.measurement is GateResult.FAIL
    assert "noise_outlier_ratio_exceeded" in result.failure_codes
    assert result.statistics["noise"]["sample_count"] == 300
    assert result.statistics["noise"]["outlier_count"] == 20


def test_cache_must_be_cleared_and_consistent_across_probes(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    noise = suite.raw[Stage0ProbeType.NOISE]
    noise["observations"][0]["telemetry"]["cache"]["cleared_before_sample"] = False
    noise["observations"][0]["telemetry"]["cache"]["identity_hash"] = SHA_A
    suite.rewrite(Stage0ProbeType.NOISE)

    result = suite.verify()

    assert result.measurement is GateResult.FAIL
    assert "cache_not_cleared" in result.failure_codes
    assert "cache_policy_mismatch" in result.failure_codes


def test_profiler_classification_covers_full_degraded_and_none(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    raw = suite.raw[Stage0ProbeType.PROFILER]

    full = _parse_profiler(raw)
    degraded_raw = deepcopy(raw)
    degraded_raw["kernels"][0]["metadata"] = {}
    degraded_raw["kernels"][0]["python_source"] = None
    degraded = _parse_profiler(degraded_raw)
    none_raw = deepcopy(raw)
    none_raw["parser_succeeded"] = False
    none_raw["kernels"] = []
    none = _parse_profiler(none_raw)

    assert classify_profiler(full) is ProfilerCapability.FULL
    assert classify_profiler(degraded) is ProfilerCapability.DEGRADED
    assert classify_profiler(none) is ProfilerCapability.NONE


def test_hotpatch_classification_covers_runtime_overlay_and_unsafe_none(
    tmp_path: Path,
) -> None:
    suite = _build_suite(tmp_path)
    raw = suite.raw[Stage0ProbeType.HOTPATCH]

    runtime = _parse_hotpatch(raw)
    overlay_raw = deepcopy(raw)
    overlay_raw["activation_mode"] = "startup_overlay"
    overlay_raw["overlay_mount"] = {
        "source_hash": SHA_C,
        "target_path": "/opt/hcuopt/overlay",
        "read_only": True,
        "container_id": "stage0-overlay-1",
    }
    overlay = _parse_hotpatch(overlay_raw)
    unsafe_raw = deepcopy(raw)
    unsafe_raw["activated_output_hash"] = SHA_D
    unsafe = _parse_hotpatch(unsafe_raw)

    assert classify_hotpatch(runtime) is HotPatchCapability.HOT_PATCH
    assert classify_hotpatch(overlay) is HotPatchCapability.OVERLAY_ONLY
    assert classify_hotpatch(unsafe) is HotPatchCapability.NONE


def test_overlay_capability_requires_a_read_only_mount(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    raw = deepcopy(suite.raw[Stage0ProbeType.HOTPATCH])
    raw["activation_mode"] = "startup_overlay"

    with pytest.raises(ValidationError, match="read-only mount"):
        _parse_hotpatch(raw)

    for unsafe_target in ("/", "/../../etc", "/opt//overlay"):
        mounted = deepcopy(raw)
        mounted["overlay_mount"] = {
            "source_hash": SHA_C,
            "target_path": unsafe_target,
            "read_only": True,
            "container_id": "stage0-overlay-1",
        }
        with pytest.raises(ValidationError, match="normalized non-root"):
            _parse_hotpatch(mounted)


def test_hotpatch_capability_is_derived_from_hash_relations_not_booleans(
    tmp_path: Path,
) -> None:
    suite = _build_suite(tmp_path)
    raw = deepcopy(suite.raw[Stage0ProbeType.HOTPATCH])
    raw["activation_verified"] = True

    with pytest.raises(ValidationError, match="activation_verified"):
        _parse_hotpatch(raw)


def test_probe_barrier_requires_exactly_one_of_each_probe(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    references = list(suite.references.values())

    with pytest.raises(Stage0EvidenceError) as caught:
        Stage0Verifier(suite.protocol, _verifier_reader(suite.root)).verify(
            suite.context,
            references[:-1],
        )
    assert caught.value.code == "probe_barrier_incomplete"

    with pytest.raises(Stage0EvidenceError) as caught:
        Stage0Verifier(suite.protocol, _verifier_reader(suite.root)).verify(
            suite.context,
            [*references, references[0]],
        )
    assert caught.value.code == "duplicate_probe"


def test_fingerprint_schema_is_strict_and_bound(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    raw = deepcopy(suite.raw[Stage0ProbeType.FINGERPRINT])
    raw["producer_summary"] = {"hardware_ok": True}

    with pytest.raises(ValidationError, match="producer_summary"):
        FingerprintEvidenceV2.model_validate_json(canonical_json_bytes(raw))
