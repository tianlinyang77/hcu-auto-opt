from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from hcuopt.adapters.execution import CommandResult
from hcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    ArtifactManifest,
    ExecutionResult,
    SourceSnapshot,
    TargetSpec,
)
from hcuopt.domain.enums import HotPatchCapability, ProfilerCapability
from hcuopt.evaluation.stage0_verifier import (
    HotpatchEvidenceV2,
    ProfilerEvidenceV2,
    Stage0EvidenceReader,
    classify_hotpatch,
    classify_profiler,
)
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.measurement.fingerprint import stable_fingerprint
from hcuopt.runtime_probes.adapter import RuntimeProbeAdapter
from hcuopt.runtime_probes.evidence import DeploymentContentAddressedEvidencePublisher
from hcuopt.runtime_probes.overlay import OverlayCapabilityProbe
from hcuopt.runtime_probes.profile import RuntimeProbeProfile
from hcuopt.runtime_probes.profiler import ProfilerCapabilityProbe
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path
from hcuopt.targets import load_target, target_fingerprint

ROOT = Path(__file__).parents[2]
TARGET = load_target(ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml")


class _Runner:
    def __init__(self, outputs: list[bytes]) -> None:
        self.outputs = outputs

    def run(self, argv: tuple[str, ...], timeout: float = 30.0) -> CommandResult:
        del timeout
        return CommandResult(argv=argv, returncode=0, stdout=self.outputs.pop(0), stderr=b"")


class _Cleaner:
    def fence(self, resource_id: str, fencing_token: int):
        return {
            "resource_id": resource_id,
            "fencing_token": fencing_token,
            "fenced": True,
        }

    def health_check(self, resource_id: str):
        return {"resource_id": resource_id, "healthy": True}


class _UnusedExecutor:
    def execute(self, request, target, output_dir):  # pragma: no cover
        raise AssertionError((request, target, output_dir))

    def cancel(self, request_id):  # pragma: no cover
        raise AssertionError(request_id)


class _Telemetry:
    def collect(self):
        accelerator = TARGET.execution_host.accelerator
        return {
            "device": {
                "device_index": accelerator.device_index,
                "temperature_c": 55.0,
                "hotspot_temperature_c": 55.0,
                "sclk_mhz": accelerator.expected_sclk_mhz,
                "mclk_mhz": accelerator.expected_mclk_mhz,
                "performance_level": "manual",
                "power_w": 210.0,
            },
            "cache": {
                "state": "flushed",
                "cleared_before_sample": True,
                "identity_hash": None,
            },
            "background_processes": [],
        }


class _Clock:
    def __init__(self) -> None:
        self.value = 100

    def now_ns(self) -> int:
        self.value += 100
        return self.value


def _mount_source(path: Path) -> str:
    value = path.resolve().as_posix()
    return f"/{value}" if os.name == "nt" else value


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _runtime_profile(tmp_path: Path) -> RuntimeProbeProfile:
    baseline_path = tmp_path / "baseline"
    candidate_path = tmp_path / "candidate"
    baseline_path.mkdir(parents=True)
    candidate_path.mkdir(parents=True)
    (baseline_path / "kernel.py").write_text("VALUE = 'baseline'\n", encoding="utf-8")
    (candidate_path / "kernel.py").write_text("VALUE = 'candidate'\n", encoding="utf-8")
    baseline = SourceSnapshot(
        kind="baseline",
        repository="fixture",
        commit="1" * 40,
        tree_hash="2" * 40,
        source_hash=canonical_source_hash(baseline_path),
        worktree_uri=baseline_path.as_uri(),
        clean=True,
    )
    candidate = SourceSnapshot(
        kind="candidate",
        repository="fixture",
        commit="1" * 40,
        tree_hash="3" * 40,
        source_hash=canonical_source_hash(candidate_path),
        worktree_uri=candidate_path.as_uri(),
        clean=True,
        parent_snapshot_id=baseline.snapshot_id,
    )
    artifact_path = tmp_path / "candidate.py"
    artifact_path.write_bytes(b"VALUE = 'candidate'\n")
    artifact = ArtifactManifest(
        candidate_id=uuid4(),
        kind="python_overlay",
        uri=artifact_path.as_uri(),
        content_hash="sha256:" + hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        source_snapshot_id=candidate.snapshot_id,
    )
    artifact_path.chmod(0o444)

    def phase(name: str):
        return {
            "argv": ["python", "probe.py", name],
            "working_directory": "/workspace",
            "environment": {"HCUOPT_CANDIDATE_CACHE_DIR": f"/tmp/cache-{name}"},
        }

    return RuntimeProbeProfile(
        profile="nmz36-stage0-v2",
        target_id=TARGET.target_id,
        target_fingerprint=target_fingerprint(TARGET),
        profiler={
            "tool_candidates": [
                {
                    "name": "rocprof",
                    "version_argv": ["rocprof", "--version"],
                    "profile_argv": ["rocprof", "--stats"],
                    "output_format": "csv",
                }
            ]
        },
        hotpatch={
            "workload_kind": "sglang_python_triton",
            "replacement_point": "/opt/hcuopt/kernel.py",
            "baseline_source": baseline,
            "candidate_source": candidate,
            "artifact": artifact,
            "overlay_mount_target": "/opt/hcuopt/kernel.py",
            "activation_marker": "candidate-v1",
            "baseline": phase("baseline"),
            "candidate": phase("candidate"),
            "recovery": phase("recovery"),
        },
    )


def _payload() -> dict[str, object]:
    return {
        "task_id": str(uuid4()),
        "stage0_run_id": str(uuid4()),
        "target_snapshot_id": str(uuid4()),
        "target_fingerprint": target_fingerprint(TARGET),
        "target": TARGET.model_dump(mode="json"),
        "workload_id": "sglang-qwen-stage0",
        "adapter_profile": "nmz36-stage0-v2",
        "probe_type": "profiler",
        "protocol_version": "s0-g0-v1",
        "mode": "formal",
        "_job_context": {
            "lease_id": str(uuid4()),
            "lease_scope": "exclusive",
            "resource_id": "hcu-7",
            "fencing_token": 19,
        },
    }


class _FormalOverlayExecutor:
    provenance = AdapterProvenance(
        profile="nmz36-stage0-v2",
        capability="executor",
        adapter_name="FormalOverlayExecutor",
        adapter_version="1",
        implementation_kind="real",
    )

    def __init__(
        self,
        evidence_directories: list[Path],
        baseline_implementation: Path,
        artifact_hash: str,
    ) -> None:
        self.evidence_directories = evidence_directories
        self.baseline_hash = _hash_bytes(baseline_implementation.read_bytes())
        self.artifact_hash = artifact_hash
        self.ordinal = 0

    def execute(self, request, target, output_dir):
        phase = ("baseline", "candidate", "recovery")[self.ordinal]
        process_id = 30_001 + self.ordinal
        start_ticks = 80_001 + self.ordinal
        evidence_dir = self.evidence_directories[self.ordinal]
        candidate = phase == "candidate"
        implementation_hash = self.artifact_hash if candidate else self.baseline_hash
        normalized = canonical_json_bytes({"text": "fixed", "tokens": [1, 2, 3]})
        (evidence_dir / "normalized-output.json").write_bytes(normalized)
        (evidence_dir / "cache-namespace.json").write_bytes(
            canonical_json_bytes(
                {"cache_directory": request.environment["HCUOPT_CANDIDATE_CACHE_DIR"]}
            )
        )
        fields = " ".join(str(value) for value in range(4, 22))
        proc_stat = f"{process_id} (stage0-overlay) S {fields} {start_ticks}"
        for event, captured in (("started", 1_000), ("reaped", 1_100)):
            (
                evidence_dir / f"process-{'start' if event == 'started' else 'exit'}.json"
            ).write_bytes(
                canonical_json_bytes(
                    {
                        "schema_version": "process-lifecycle-v1",
                        "event": event,
                        "restart_ordinal": self.ordinal,
                        "observer_process_id": 29_000,
                        "process_id": process_id,
                        "proc_stat_line": proc_stat,
                        "captured_monotonic_ns": captured + self.ordinal * 1_000,
                        "waitpid_result_pid": process_id if event == "reaped" else None,
                        "wait_status": 0 if event == "reaped" else None,
                    }
                )
            )
        observation = {
            "protocol_version": "hcuopt-overlay-result-v2",
            "activation_marker": "candidate-v1" if candidate else "baseline",
            "output_hash": _hash_bytes(normalized),
            "workload_kind": "sglang_python_triton",
            "replacement_point": "/opt/hcuopt/kernel.py",
            "implementation_hash": implementation_hash,
            "process_id": process_id,
        }
        if candidate:
            observation["loaded_artifact_hash"] = self.artifact_hash
        attempt = output_dir / "scripted" / phase
        attempt.mkdir(parents=True)
        stdout = attempt / "stdout.json"
        stdout.write_bytes(canonical_json_bytes(observation))
        now = datetime.now(timezone.utc)
        result = ExecutionResult(
            request_id=request.request_id,
            status="succeeded",
            exit_code=0,
            started_at=now,
            finished_at=now,
            stdout_uri=stdout.as_uri(),
            metadata={
                "target_id": target.target_id,
                "resource_id": request.resource_id,
                "fencing_token": request.fencing_token,
                "container_image": request.container_image,
                "container_name": f"stage0-overlay-{phase}",
            },
            adapter_provenance=self.provenance,
            synthetic=False,
        )
        self.ordinal += 1
        return result

    def cancel(self, request_id):  # pragma: no cover
        return {"request_id": str(request_id), "cancelled": True}


def _formal_hotpatch_target_and_profile(
    tmp_path: Path,
) -> tuple[TargetSpec, RuntimeProbeProfile, list[Path], Path, str]:
    baseline_path = tmp_path / "baseline"
    candidate_path = tmp_path / "candidate"
    baseline_path.mkdir(parents=True)
    candidate_path.mkdir()
    baseline_implementation = baseline_path / "kernel.py"
    baseline_implementation.write_bytes(b"VALUE = 'baseline'\n")
    (candidate_path / "kernel.py").write_bytes(b"VALUE = 'candidate'\n")
    target = TARGET.model_copy(
        update={
            "source_baseline": TARGET.source_baseline.model_copy(
                update={"clean_checkout": _mount_source(baseline_path)}
            )
        }
    )
    baseline = SourceSnapshot(
        kind="baseline",
        repository=target.source_baseline.repository,
        commit=target.source_baseline.commit,
        tree_hash="a" * 40,
        source_hash=canonical_source_hash(baseline_path),
        worktree_uri=baseline_path.as_uri(),
        clean=True,
    )
    candidate = SourceSnapshot(
        kind="candidate",
        repository=baseline.repository,
        commit=baseline.commit,
        tree_hash="b" * 40,
        source_hash=canonical_source_hash(candidate_path),
        worktree_uri=candidate_path.as_uri(),
        clean=True,
        parent_snapshot_id=baseline.snapshot_id,
    )
    artifact_path = tmp_path / "candidate-overlay.py"
    artifact_path.write_bytes(b"VALUE = 'candidate'\n")
    artifact_hash = _hash_bytes(artifact_path.read_bytes())
    artifact = ArtifactManifest(
        candidate_id=uuid4(),
        kind="python_overlay",
        uri=artifact_path.as_uri(),
        content_hash=artifact_hash,
        source_snapshot_id=candidate.snapshot_id,
    )
    artifact_path.chmod(0o444)
    evidence_directories = [tmp_path / f"phase-{name}" for name in ("b", "c", "r")]
    for directory in evidence_directories:
        directory.mkdir()

    def phase(
        name: str,
        evidence_dir: Path,
        implementation: Path,
        cache: str,
    ) -> dict[str, object]:
        container_evidence = f"/evidence/{name}"
        return {
            "argv": [
                "python",
                "/opt/hcuopt/sglang_overlay_runner.py",
                "--phase",
                name,
                "--evidence-dir",
                container_evidence,
                "--formal-evidence",
            ],
            "working_directory": "/workspace",
            "environment": {"HCUOPT_CANDIDATE_CACHE_DIR": cache},
            "mounts": [
                {
                    "source": _mount_source(evidence_dir),
                    "target": container_evidence,
                    "read_only": False,
                }
            ],
            "evidence_directory_uri": evidence_dir.as_uri(),
            "implementation_source_uri": implementation.as_uri(),
        }

    profile = RuntimeProbeProfile(
        profile="nmz36-stage0-v2",
        target_id=target.target_id,
        target_fingerprint=target_fingerprint(target),
        profiler={
            "tool_candidates": [
                {
                    "name": "rocprof",
                    "version_argv": ["rocprof", "--version"],
                    "profile_argv": ["rocprof", "--stats"],
                    "output_format": "csv",
                }
            ]
        },
        hotpatch={
            "workload_kind": "sglang_python_triton",
            "replacement_point": "/opt/hcuopt/kernel.py",
            "baseline_source": baseline,
            "candidate_source": candidate,
            "artifact": artifact,
            "overlay_mount_target": "/opt/hcuopt/kernel.py",
            "activation_marker": "candidate-v1",
            "baseline": phase(
                "baseline",
                evidence_directories[0],
                baseline_implementation,
                "/tmp/cache-baseline",
            ),
            "candidate": phase(
                "candidate",
                evidence_directories[1],
                artifact_path,
                "/tmp/cache-candidate",
            ),
            "recovery": phase(
                "recovery",
                evidence_directories[2],
                baseline_implementation,
                "/tmp/cache-baseline",
            ),
        },
    )
    return target, profile, evidence_directories, baseline_implementation, artifact_hash


def _hotpatch_payload(target: TargetSpec) -> dict[str, object]:
    payload = _payload()
    payload.update(
        {
            "target_fingerprint": target_fingerprint(target),
            "target": target.model_dump(mode="json"),
            "probe_type": "hotpatch",
        }
    )
    return payload


def test_formal_profiler_publishes_original_rocprof_bytes(tmp_path: Path) -> None:
    raw_csv = (
        b"Kernel_Name,Start_Timestamp,End_Timestamp,GPU_ID,Queue_ID\nhgemm_128x128,100,1000,7,1\n"
    )
    cleaner = _Cleaner()
    evidence_root = tmp_path / "trusted"
    adapter = RuntimeProbeAdapter(
        ProfilerCapabilityProbe(_Runner([b"rocprofiler-sdk 0.6\n", raw_csv])),
        OverlayCapabilityProbe(_UnusedExecutor(), cleaner),
        cleaner,
        TARGET,
        _runtime_profile(tmp_path / "profile"),
        DeploymentContentAddressedEvidencePublisher(evidence_root),
        _Telemetry(),
        _Clock(),
    )

    output = adapter.run_probe(_payload(), tmp_path / "worker-output")
    envelope = ProfilerEvidenceV2.model_validate_json(
        file_uri_to_path(output.raw_evidence_uri).read_bytes()
    )

    assert envelope.binding.run_mode.value == "formal"
    assert envelope.tool_name == "rocprof"
    assert file_uri_to_path(envelope.raw_output.uri).read_bytes() == raw_csv
    assert output.summary["capability"] == "none"
    assert output.cleanup_evidence == {
        "fence": {"resource_id": "hcu-7", "fencing_token": 19, "fenced": True},
        "health": {"resource_id": "hcu-7", "healthy": True},
    }

    if os.name == "posix":
        assert (
            classify_profiler(
                envelope,
                Stage0EvidenceReader(evidence_root),
            )
            is ProfilerCapability.DEGRADED
        )


def test_formal_profiler_binding_uses_target_stable_identity(tmp_path: Path) -> None:
    cleaner = _Cleaner()
    adapter = RuntimeProbeAdapter(
        ProfilerCapabilityProbe(
            _Runner(
                [
                    b"rocprofiler-sdk 0.6\n",
                    b"Kernel_Name,Start_Timestamp,End_Timestamp\n",
                ]
            )
        ),
        OverlayCapabilityProbe(_UnusedExecutor(), cleaner),
        cleaner,
        TARGET,
        _runtime_profile(tmp_path / "profile"),
        DeploymentContentAddressedEvidencePublisher(tmp_path / "trusted"),
        _Telemetry(),
        _Clock(),
    )

    output = adapter.run_probe(_payload(), tmp_path / "worker-output")
    envelope = ProfilerEvidenceV2.model_validate_json(
        file_uri_to_path(output.raw_evidence_uri).read_bytes()
    )
    assert envelope.binding.environment_fingerprint == stable_fingerprint(
        TARGET.model_dump(mode="json")
    )


@pytest.mark.parametrize("publisher", [None])
def test_formal_profiler_fails_before_commands_without_authority(
    tmp_path: Path,
    publisher,
) -> None:
    runner = _Runner([])
    cleaner = _Cleaner()
    adapter = RuntimeProbeAdapter(
        ProfilerCapabilityProbe(runner),
        OverlayCapabilityProbe(_UnusedExecutor(), cleaner),
        cleaner,
        TARGET,
        _runtime_profile(tmp_path / "profile"),
        publisher,
        _Telemetry(),
        _Clock(),
    )
    with pytest.raises(ValueError, match="deployment-authorized"):
        adapter.run_probe(_payload(), tmp_path / "worker-output")
    assert runner.outputs == []


def test_formal_hotpatch_publishes_three_independently_verifiable_phases(
    tmp_path: Path,
) -> None:
    target, profile, phase_dirs, baseline_implementation, artifact_hash = (
        _formal_hotpatch_target_and_profile(tmp_path / "profile")
    )
    cleaner = _Cleaner()
    executor = _FormalOverlayExecutor(
        phase_dirs,
        baseline_implementation,
        artifact_hash,
    )
    evidence_root = tmp_path / "trusted"
    adapter = RuntimeProbeAdapter(
        ProfilerCapabilityProbe(_Runner([])),
        OverlayCapabilityProbe(executor, cleaner),
        cleaner,
        target,
        profile,
        DeploymentContentAddressedEvidencePublisher(evidence_root),
        _Telemetry(),
        _Clock(),
    )

    output = adapter.run_probe(
        _hotpatch_payload(target),
        tmp_path / "worker-output",
    )
    envelope = HotpatchEvidenceV2.model_validate_json(
        file_uri_to_path(output.raw_evidence_uri).read_bytes()
    )

    assert envelope.activation_mode == "startup_overlay"
    assert envelope.overlay_mount is not None
    assert envelope.overlay_mount.read_only
    assert output.summary["sglang_overlay_proved"] is True
    assert (
        len(
            {
                envelope.baseline_state.sha256,
                envelope.candidate_state.sha256,
                envelope.recovery_state.sha256,
            }
        )
        == 3
    )
    assert envelope.artifact is not None
    assert envelope.overlay_mount.source_uri == envelope.artifact.uri

    if os.name == "posix":
        assert (
            classify_hotpatch(
                envelope,
                Stage0EvidenceReader(evidence_root),
                target,
            )
            is HotPatchCapability.OVERLAY_ONLY
        )
