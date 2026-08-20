from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from hcuopt.adapters.execution import CommandResult
from hcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    ArtifactManifest,
    ExecutionRequest,
    ExecutionResult,
    SourceSnapshot,
)
from hcuopt.domain.enums import LeaseScope
from hcuopt.domain.errors import ExecutionSafetyError
from hcuopt.runtime_probes.adapter import RuntimeProbeAdapter
from hcuopt.runtime_probes.evidence import (
    LocalContentAddressedEvidencePublisher,
    write_immutable_json,
)
from hcuopt.runtime_probes.overlay import (
    CACHE_ENVIRONMENT_KEY,
    OVERLAY_RESULT_PROTOCOL_VERSION,
    OverlayCapabilityProbe,
)
from hcuopt.runtime_probes.profile import RuntimeProbeProfile
from hcuopt.runtime_probes.profiler import ProfilerCapabilityProbe
from hcuopt.source_hash import canonical_source_hash
from hcuopt.targets import load_target, target_fingerprint

ROOT = Path(__file__).parents[2]
TARGET = load_target(ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml")


class ScriptedRunner:
    def __init__(self, results: list[CommandResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...], timeout: float = 30.0) -> CommandResult:
        del timeout
        self.calls.append(tuple(argv))
        return self.results.pop(0)

    def popen(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("not used by profiler capability tests")


def _command(argv: tuple[str, ...], *, stdout: bytes = b"", returncode: int = 0) -> CommandResult:
    return CommandResult(argv=argv, returncode=returncode, stdout=stdout, stderr=b"")


def test_profiler_reports_full_only_when_every_required_field_is_observed(
    tmp_path: Path,
) -> None:
    for name in (
        "terminal_commands.log",
        "analysis_stdout.txt",
        "torch_profiler_analysis.md",
    ):
        (tmp_path / name).write_text(name, encoding="utf-8")
    records = [
        {
            "kernel_name": "rms_norm",
            "duration_value": 12.5,
            "duration_unit": "us",
            "call_count": 4,
            "kernel_category": "kernel",
            "shape": [1, 4096],
            "dtype": "bf16",
            "meta": {"stage": "decode"},
            "python_location": "rmsnorm.py:10",
            "hip_location": "hipModuleLaunchKernel",
        }
    ]
    runner = ScriptedRunner(
        [
            _command(("profile-tool", "--version"), stdout=b"profile-tool 1\n"),
            _command(("profile-tool", "--json"), stdout=json.dumps(records).encode()),
        ]
    )

    result = ProfilerCapabilityProbe(runner).run(
        {
            "tool_candidates": [
                {
                    "name": "profile-llm-torch",
                    "version_argv": ["profile-tool", "--version"],
                    "profile_argv": ["profile-tool", "--json"],
                    "output_format": "json",
                }
            ],
            "triage_work_dir": str(tmp_path),
        }
    )

    assert result["capability"] == "full"
    assert result["capture_contract"] == {
        "workflow": "profile-llm-torch/triage",
        "profile_workload": "both",
        "warmup_steps": 10,
        "num_steps": 5,
        "prefill_input_len": 4090,
        "prefill_output_len": 1,
        "decode_input_len": 1,
        "decode_output_len": 2048,
    }
    assert {item["name"] for item in result["triage_artifacts"]} == {
        "terminal_commands.log",
        "analysis_stdout.txt",
        "torch_profiler_analysis.md",
    }


def test_profiler_degrades_instead_of_inventing_missing_shape_or_source() -> None:
    raw = (
        b"kernel_name,duration_value,duration_unit,call_count,kernel_category\n"
        b"rms_norm,12.5,us,4,kernel\n"
    )
    runner = ScriptedRunner(
        [
            _command(("rocprof", "--version"), stdout=b"rocprof 1\n"),
            _command(("rocprof", "--csv"), stdout=raw),
        ]
    )

    result = ProfilerCapabilityProbe(runner).run(
        {
            "tool_candidates": [
                {
                    "name": "rocprof",
                    "version_argv": ["rocprof", "--version"],
                    "profile_argv": ["rocprof", "--csv"],
                    "output_format": "csv",
                }
            ]
        }
    )

    assert result["capability"] == "degraded"
    assert "shape" in result["missing_fields"]
    assert "python_location" in result["missing_fields"]
    assert "shape" not in result["records"][0]


def test_profiler_does_not_combine_fields_across_different_records() -> None:
    records = [
        {
            "kernel_name": "real_kernel",
            "duration_us": 12.5,
            "call_count": 4,
            "kernel_category": "kernel",
        },
        {
            "shape": [1, 4096],
            "dtype": "bf16",
            "meta": {"stage": "decode"},
            "python_location": "rmsnorm.py:10",
            "hip_location": "hipModuleLaunchKernel",
        },
    ]
    runner = ScriptedRunner(
        [
            _command(("profiler", "--version"), stdout=b"profiler 1\n"),
            _command(("profiler", "--json"), stdout=json.dumps(records).encode()),
        ]
    )

    result = ProfilerCapabilityProbe(runner).run(
        {
            "tool_candidates": [
                {
                    "name": "profiler",
                    "version_argv": ["profiler", "--version"],
                    "profile_argv": ["profiler", "--json"],
                    "output_format": "json",
                }
            ]
        }
    )

    assert result["capability"] == "degraded"
    assert result["valid_record_count"] == 1


@pytest.mark.parametrize(
    "record",
    [
        {"kernel_name": "k", "duration_us": 1.0, "call_count": 1},
        {
            "kernel_name": "k",
            "duration_us": float("nan"),
            "call_count": 1,
            "kernel_category": "kernel",
        },
        {
            "kernel_name": "k",
            "duration_us": 1.0,
            "call_count": True,
            "kernel_category": "kernel",
        },
        {
            "kernel_name": "memcpy",
            "duration_us": 1.0,
            "call_count": 1,
            "kernel_category": "runtime",
        },
    ],
)
def test_profiler_rejects_non_kernel_or_invalid_core_records(
    record: dict[str, Any],
) -> None:
    runner = ScriptedRunner(
        [
            _command(("profiler", "--version"), stdout=b"profiler 1\n"),
            _command(("profiler", "--json"), stdout=json.dumps([record]).encode()),
        ]
    )
    result = ProfilerCapabilityProbe(runner).run(
        {
            "tool_candidates": [
                {
                    "name": "profiler",
                    "version_argv": ["profiler", "--version"],
                    "profile_argv": ["profiler", "--json"],
                    "output_format": "json",
                }
            ]
        }
    )

    assert result["capability"] == "none"
    assert result["valid_record_count"] == 0


def test_profiler_rejects_legacy_mixed_workload_capture() -> None:
    with pytest.raises(ValueError, match="profile_workload"):
        ProfilerCapabilityProbe(ScriptedRunner([])).run(
            {
                "profile_workload": "legacy",
                "tool_candidates": [
                    {
                        "name": "fixture",
                        "version_argv": ["fixture", "--version"],
                        "profile_argv": ["fixture", "--profile"],
                        "output_format": "json",
                    }
                ],
            }
        )


def test_profiler_reads_rank_local_torch_trace_without_inventing_details(
    tmp_path: Path,
) -> None:
    trace_path = tmp_path / "rank-0.trace.json.gz"
    with gzip.open(trace_path, "wt", encoding="utf-8") as trace:
        json.dump(
            {
                "traceEvents": [
                    {
                        "ph": "X",
                        "cat": "kernel",
                        "name": "rms_norm_kernel",
                        "dur": 4.0,
                        "args": {"stream": 7, "Input Dims": [[1, 4096]]},
                    },
                    {
                        "ph": "X",
                        "cat": "kernel",
                        "name": "rms_norm_kernel",
                        "dur": 6.0,
                        "args": {"stream": 7},
                    },
                ]
            },
            trace,
        )
    runner = ScriptedRunner(
        [
            _command(("analyze", "--help"), stdout=b"usage"),
            _command(("triage", str(trace_path)), stdout=b"report written"),
        ]
    )

    result = ProfilerCapabilityProbe(runner).run(
        {
            "tool_candidates": [
                {
                    "name": "profile-llm-torch",
                    "version_argv": ["analyze", "--help"],
                    "profile_argv": ["triage", str(trace_path)],
                    "output_format": "torch_trace",
                    "output_path": str(trace_path),
                }
            ]
        }
    )

    assert result["capability"] == "degraded"
    assert result["records"] == [
        {
            "kernel_name": "rms_norm_kernel",
            "duration_value": 10.0,
            "duration_unit": "us",
            "call_count": 2,
            "kernel_category": "kernel",
            "shape": [[1, 4096]],
        }
    ]


class RecordingProfilerExecutor:
    provenance = AdapterProvenance(
        profile="nmz36-stage0-v2",
        capability="executor",
        adapter_name="RecordingProfilerExecutor",
        adapter_version="1",
        implementation_kind="real",
    )

    def __init__(self, outputs: list[bytes]) -> None:
        self.outputs = outputs
        self.requests: list[ExecutionRequest] = []

    def execute(
        self, request: ExecutionRequest, target: Any, output_dir: Path
    ) -> ExecutionResult:
        del target
        self.requests.append(request)
        attempt = output_dir / str(request.request_id)
        attempt.mkdir(parents=True)
        stdout = attempt / "stdout.log"
        stderr = attempt / "stderr.log"
        stdout.write_bytes(self.outputs.pop(0))
        stderr.write_bytes(b"")
        now = datetime.now(timezone.utc)
        return ExecutionResult(
            request_id=request.request_id,
            status="succeeded",
            exit_code=0,
            started_at=now,
            finished_at=now,
            stdout_uri=stdout.resolve().as_uri(),
            stderr_uri=stderr.resolve().as_uri(),
            adapter_provenance=self.provenance,
        )

    def cancel(self, request_id: Any) -> dict[str, Any]:
        return {"request_id": str(request_id), "cancelled": True}


def test_real_profiler_commands_run_in_digest_locked_container(tmp_path: Path) -> None:
    records = [
        {
            "kernel_name": "kernel",
            "duration_value": 1,
            "duration_unit": "us",
            "call_count": 1,
            "kernel_category": "kernel",
        }
    ]
    executor = RecordingProfilerExecutor([b"tool 1\n", json.dumps(records).encode()])

    result = ProfilerCapabilityProbe(executor=executor).run(
        {
            "tool_candidates": [
                {
                    "name": "profile-llm-torch",
                    "version_argv": ["python", "analyze.py", "--help"],
                    "profile_argv": ["python", "probe.py"],
                    "output_format": "json",
                }
            ]
        },
        target=TARGET,
        output_dir=tmp_path,
        resource_id="hcu-7",
        fencing_token=12,
    )

    assert result["capability"] == "degraded"
    assert len(executor.requests) == 2
    for request in executor.requests:
        assert request.container_image == TARGET.inference_image.immutable_reference
        assert request.resource_id == "hcu-7"
        assert request.fencing_token == 12
        assert request.lease_scope is LeaseScope.EXCLUSIVE


class ScriptedExecutor:
    provenance = AdapterProvenance(
        profile="nmz36-stage0-v2",
        capability="executor",
        adapter_name="ScriptedExecutor",
        adapter_version="1",
        implementation_kind="real",
    )

    def __init__(self, metadata: list[dict[str, Any]]) -> None:
        self.metadata = metadata

    def execute(self, request: ExecutionRequest, target: Any, output_dir: Path) -> ExecutionResult:
        del target, output_dir
        now = datetime.now(timezone.utc)
        return ExecutionResult(
            request_id=request.request_id,
            status="succeeded",
            exit_code=0,
            started_at=now,
            finished_at=now,
            metadata=self.metadata.pop(0),
            adapter_provenance=self.provenance,
            synthetic=False,
        )

    def cancel(self, request_id: Any) -> dict[str, Any]:
        return {"request_id": str(request_id), "cancelled": True}


class ScriptedCleaner:
    provenance = AdapterProvenance(
        profile="nmz36-stage0-v2",
        capability="resource_cleaner",
        adapter_name="ScriptedCleaner",
        adapter_version="1",
        implementation_kind="real",
    )

    def __init__(self, healthy: list[bool]) -> None:
        self.healthy = healthy

    def health_check(self, resource_id: str) -> dict[str, Any]:
        return {"resource_id": resource_id, "healthy": self.healthy.pop(0)}

    def fence(self, resource_id: str, fencing_token: int) -> dict[str, Any]:
        return {"resource_id": resource_id, "fencing_token": fencing_token, "fenced": True}


class StdoutScriptedExecutor(ScriptedExecutor):
    def execute(self, request: ExecutionRequest, target: Any, output_dir: Path) -> ExecutionResult:
        result = super().execute(request, target, output_dir)
        stdout = output_dir / f"{request.request_id}.json"
        stdout.parent.mkdir(parents=True, exist_ok=True)
        stdout.write_text(json.dumps(result.metadata), encoding="utf-8")
        return result.model_copy(update={"stdout_uri": stdout.resolve().as_uri(), "metadata": {}})


def _overlay_fixture(
    tmp_path: Path, *, workload_kind: str = "sglang_python_triton"
) -> tuple[dict[str, Any], str]:
    baseline_path = tmp_path / "baseline"
    candidate_path = tmp_path / "candidate"
    baseline_path.mkdir()
    candidate_path.mkdir()
    (baseline_path / "module.py").write_text("VALUE = 'baseline'\n", encoding="utf-8")
    (candidate_path / "module.py").write_text("VALUE = 'candidate'\n", encoding="utf-8")
    baseline = SourceSnapshot(
        kind="baseline",
        repository="fixture",
        commit="1" * 40,
        tree_hash="2" * 40,
        source_hash=canonical_source_hash(baseline_path),
        worktree_uri=baseline_path.resolve().as_uri(),
        clean=True,
    )
    candidate = SourceSnapshot(
        kind="candidate",
        repository="fixture",
        commit="3" * 40,
        tree_hash="4" * 40,
        source_hash=canonical_source_hash(candidate_path),
        worktree_uri=candidate_path.resolve().as_uri(),
        clean=True,
        parent_snapshot_id=baseline.snapshot_id,
    )
    artifact_path = tmp_path / "candidate.tar"
    artifact_path.write_bytes(b"candidate-overlay")
    artifact_hash = "sha256:" + hashlib.sha256(b"candidate-overlay").hexdigest()
    artifact = ArtifactManifest(
        candidate_id=uuid4(),
        kind="python-overlay",
        uri=artifact_path.resolve().as_uri(),
        content_hash=artifact_hash,
        source_snapshot_id=candidate.snapshot_id,
    )
    artifact_path.chmod(0o444)
    target_path = "/opt/hcuopt/candidate.tar"

    def phase(name: str) -> dict[str, Any]:
        return {
            "argv": ["python", "probe.py", name],
            "working_directory": "/workspace",
            "environment": {CACHE_ENVIRONMENT_KEY: f"/tmp/hcuopt-cache-{name}"},
        }

    return (
        {
            "workload_kind": workload_kind,
            "replacement_point": target_path,
            "baseline_source": baseline.model_dump(mode="json"),
            "candidate_source": candidate.model_dump(mode="json"),
            "artifact": artifact.model_dump(mode="json"),
            "baseline": phase("baseline"),
            "candidate": phase("candidate"),
            "recovery": phase("recovery"),
            "activation_marker": "candidate-v1",
            "overlay_mount_target": target_path,
        },
        artifact_hash,
    )


def _overlay_observations(
    artifact_hash: str, *, workload_kind: str = "sglang_python_triton"
) -> list[dict[str, Any]]:
    output_hash = "sha256:" + hashlib.sha256(b"same output").hexdigest()
    baseline_hash = "sha256:" + hashlib.sha256(b"baseline implementation").hexdigest()
    common = {
        "protocol_version": OVERLAY_RESULT_PROTOCOL_VERSION,
        "output_hash": output_hash,
        "workload_kind": workload_kind,
        "replacement_point": "/opt/hcuopt/candidate.tar",
    }
    return [
        {
            **common,
            "activation_marker": "baseline",
            "implementation_hash": baseline_hash,
            "process_id": 101,
        },
        {
            **common,
            "activation_marker": "candidate-v1",
            "loaded_artifact_hash": artifact_hash,
            "implementation_hash": artifact_hash,
            "process_id": 102,
        },
        {
            **common,
            "activation_marker": "baseline",
            "implementation_hash": baseline_hash,
            "process_id": 103,
        },
    ]


def _runtime_profile(tmp_path: Path) -> RuntimeProbeProfile:
    hotpatch, _ = _overlay_fixture(tmp_path)
    return RuntimeProbeProfile(
        profile="nmz36-stage0-v2",
        target_id=TARGET.target_id,
        target_fingerprint=target_fingerprint(TARGET),
        profiler={
            "tool_candidates": [
                {
                    "name": "frozen-profiler",
                    "version_argv": ["frozen-profiler", "--version"],
                    "profile_argv": ["frozen-profiler", "--json"],
                    "output_format": "json",
                }
            ]
        },
        hotpatch=hotpatch,
    )


def test_runtime_adapter_uses_frozen_profile_not_job_commands(tmp_path: Path) -> None:
    records = [
        {
            "kernel_name": "kernel",
            "duration_us": 1.0,
            "call_count": 1,
            "kernel_category": "kernel",
        }
    ]
    runner = ScriptedRunner(
        [
            _command(("frozen-profiler", "--version"), stdout=b"version\n"),
            _command(
                ("frozen-profiler", "--json"), stdout=json.dumps(records).encode()
            ),
        ]
    )
    cleaner = ScriptedCleaner([])
    adapter = RuntimeProbeAdapter(
        ProfilerCapabilityProbe(runner),
        OverlayCapabilityProbe(ScriptedExecutor([]), cleaner),
        cleaner,
        TARGET,
        _runtime_profile(tmp_path),
    )

    output = adapter.run_probe(
        {
            "stage0_run_id": str(uuid4()),
            "target_snapshot_id": str(uuid4()),
            "target_fingerprint": target_fingerprint(TARGET),
            "target": TARGET.model_dump(mode="json"),
            "probe_type": "profiler",
            "protocol_version": "fixture-v1",
            "mode": "dry_run",
            "runtime_probe": {
                "profiler": {"profile_argv": ["sh", "-c", "untrusted"]}
            },
            "_job_context": {},
        },
        tmp_path / "evidence",
    )

    assert runner.calls == [
        ("frozen-profiler", "--version"),
        ("frozen-profiler", "--json"),
    ]
    assert output.summary["capability"] == "degraded"


def test_formal_runtime_probe_rejects_worker_local_evidence_store(tmp_path: Path) -> None:
    cleaner = ScriptedCleaner([])
    adapter = RuntimeProbeAdapter(
        ProfilerCapabilityProbe(ScriptedRunner([])),
        OverlayCapabilityProbe(ScriptedExecutor([]), cleaner),
        cleaner,
        TARGET,
        _runtime_profile(tmp_path),
    )

    with pytest.raises(ValueError, match="verifier-owned evidence publisher"):
        adapter.run_probe(
            {
                "stage0_run_id": str(uuid4()),
                "target_snapshot_id": str(uuid4()),
                "target_fingerprint": target_fingerprint(TARGET),
                "target": TARGET.model_dump(mode="json"),
                "probe_type": "profiler",
                "protocol_version": "fixture-v1",
                "mode": "formal",
                "_job_context": {
                    "lease_id": str(uuid4()),
                    "resource_id": "hcu-7",
                    "fencing_token": 9,
                },
            },
            tmp_path / "evidence",
        )


def test_formal_runtime_probe_records_lease_cleanup_with_trusted_publisher(
    tmp_path: Path,
) -> None:
    class VerifierOwnedPublisher(LocalContentAddressedEvidencePublisher):
        publication_authority = "fixture-verifier-owned"
        authorizes_formal_results = True

    records = [
        {
            "kernel_name": "kernel",
            "duration_us": 1.0,
            "call_count": 1,
            "kernel_category": "kernel",
        }
    ]
    runner = ScriptedRunner(
        [
            _command(("frozen-profiler", "--version"), stdout=b"version\n"),
            _command(
                ("frozen-profiler", "--json"), stdout=json.dumps(records).encode()
            ),
        ]
    )
    cleaner = ScriptedCleaner([True])
    adapter = RuntimeProbeAdapter(
        ProfilerCapabilityProbe(runner),
        OverlayCapabilityProbe(ScriptedExecutor([]), cleaner),
        cleaner,
        TARGET,
        _runtime_profile(tmp_path),
        VerifierOwnedPublisher(),
    )
    lease_id = str(uuid4())

    output = adapter.run_probe(
        {
            "stage0_run_id": str(uuid4()),
            "target_snapshot_id": str(uuid4()),
            "target_fingerprint": target_fingerprint(TARGET),
            "target": TARGET.model_dump(mode="json"),
            "probe_type": "profiler",
            "protocol_version": "fixture-v1",
            "mode": "formal",
            "_job_context": {
                "lease_id": lease_id,
                "resource_id": "hcu-7",
                "fencing_token": 9,
            },
        },
        tmp_path / "evidence",
    )

    assert output.cleanup_evidence == {
        "fence": {"resource_id": "hcu-7", "fencing_token": 9, "fenced": True},
        "health": {"resource_id": "hcu-7", "healthy": True},
    }
    evidence = json.loads(Path(output.raw_evidence_uri.removeprefix("file://")).read_text())
    assert evidence["execution_context"]["lease_id"] == lease_id
    assert evidence["publication_authority"] == "fixture-verifier-owned"


def test_overlay_reports_overlay_only_after_activation_correctness_and_recovery(
    tmp_path: Path,
) -> None:
    configuration, artifact_hash = _overlay_fixture(tmp_path)
    executor = ScriptedExecutor(_overlay_observations(artifact_hash))
    result = OverlayCapabilityProbe(executor, ScriptedCleaner([True, True, True])).run(
        configuration,
        target=TARGET,
        output_dir=tmp_path / "results",
        resource_id="hcu-7",
        fencing_token=9,
    )

    assert result["capability"] == "overlay_only"
    assert result["activation_proved"] is True
    assert result["correctness_passed"] is True
    assert result["recovery_passed"] is True


def test_overlay_reads_versioned_observations_from_execution_stdout(tmp_path: Path) -> None:
    configuration, artifact_hash = _overlay_fixture(tmp_path)
    executor = StdoutScriptedExecutor(_overlay_observations(artifact_hash))

    result = OverlayCapabilityProbe(executor, ScriptedCleaner([True, True, True])).run(
        configuration,
        target=TARGET,
        output_dir=tmp_path / "results",
        resource_id="hcu-7",
        fencing_token=9,
    )

    assert result["capability"] == "overlay_only"
    assert result["observations"]["candidate"]["loaded_artifact_hash"] == artifact_hash


def test_generic_artifact_mount_cannot_authorize_sglang_overlay(tmp_path: Path) -> None:
    configuration, artifact_hash = _overlay_fixture(
        tmp_path, workload_kind="generic_artifact_mount"
    )
    result = OverlayCapabilityProbe(
        ScriptedExecutor(
            _overlay_observations(
                artifact_hash, workload_kind="generic_artifact_mount"
            )
        ),
        ScriptedCleaner([True, True, True]),
    ).run(
        configuration,
        target=TARGET,
        output_dir=tmp_path / "results",
        resource_id="hcu-7",
        fencing_token=9,
    )

    assert result["capability"] == "none"
    assert result["generic_artifact_mount_passed"] is True
    assert result["sglang_overlay_proved"] is False


def test_sglang_profile_rejects_generic_runner_observations(tmp_path: Path) -> None:
    configuration, artifact_hash = _overlay_fixture(tmp_path)
    result = OverlayCapabilityProbe(
        ScriptedExecutor(
            _overlay_observations(
                artifact_hash, workload_kind="generic_artifact_mount"
            )
        ),
        ScriptedCleaner([True, True, True]),
    ).run(
        configuration,
        target=TARGET,
        output_dir=tmp_path / "results",
        resource_id="hcu-7",
        fencing_token=9,
    )

    assert result["capability"] == "none"
    assert result["observation_contract_matches"] is False


def test_overlay_rejects_unversioned_stdout_observation(tmp_path: Path) -> None:
    configuration, _ = _overlay_fixture(tmp_path)
    output_hash = "sha256:" + hashlib.sha256(b"same-output").hexdigest()
    executor = StdoutScriptedExecutor(
        [{"activation_marker": "baseline", "output_hash": output_hash}]
    )

    with pytest.raises(ExecutionSafetyError, match="protocol_version"):
        OverlayCapabilityProbe(executor, ScriptedCleaner([True])).run(
            configuration,
            target=TARGET,
            output_dir=tmp_path / "results",
            resource_id="hcu-7",
            fencing_token=9,
        )


def test_overlay_runner_proves_candidate_load_without_changing_output(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.txt"
    candidate = tmp_path / "candidate.txt"
    baseline.write_bytes(b"identical no-op bytes")
    candidate.write_bytes(baseline.read_bytes())
    runner = ROOT / "src" / "hcuopt" / "runtime_probes" / "overlay_runner.py"

    baseline_run = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--baseline-artifact",
            str(baseline),
            "--activation-marker",
            "candidate-v1",
            "--replacement-point",
            str(candidate),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    candidate_run = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--baseline-artifact",
            str(baseline),
            "--candidate-artifact",
            str(candidate),
            "--activation-marker",
            "candidate-v1",
            "--replacement-point",
            str(candidate),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    baseline_result = json.loads(baseline_run.stdout)
    candidate_result = json.loads(candidate_run.stdout)
    assert baseline_result["activation_marker"] == "baseline"
    assert candidate_result["activation_marker"] == "candidate-v1"
    assert candidate_result["loaded_artifact_hash"] == baseline_result["output_hash"]
    assert candidate_result["output_hash"] == baseline_result["output_hash"]


def test_sglang_overlay_runner_requires_import_from_server_process_group(
    tmp_path: Path,
) -> None:
    smoke_runner = tmp_path / "fake_sglang_smoke.py"
    smoke_runner.write_text(
        """\
import hashlib
import json
import os
import sys
from pathlib import Path

evidence_dir = Path(sys.argv[sys.argv.index("--evidence-dir") + 1])
evidence_dir.mkdir(parents=True)
pid = os.getpid()
group = os.getpgrp()
(evidence_dir / "start.json").write_text(json.dumps({"pid": pid, "process_group_id": group}))
(evidence_dir / "result.json").write_text(json.dumps({
    "status": "succeeded",
    "cleanup_succeeded": True,
    "normalized_output": {"text": "fixed"},
}))
marker_path = os.environ.get("HCUOPT_TEST_IMPORT_MARKER")
if marker_path:
    module = Path(os.environ["HCUOPT_TEST_REPLACEMENT"])
    digest = "sha256:" + hashlib.sha256(module.read_bytes()).hexdigest()
    Path(marker_path).write_text(json.dumps({
        "protocol_version": "hcuopt-sglang-overlay-import-v1",
        "process_id": pid,
        "process_group_id": group,
        "module_file": str(module.resolve()),
        "module_sha256": digest,
    }))
""",
        encoding="utf-8",
    )
    spec = tmp_path / "spec.json"
    spec.write_text("{}", encoding="utf-8")
    candidate = tmp_path / "candidate.py"
    candidate.write_text("VALUE = 'candidate'\n", encoding="utf-8")
    candidate_hash = "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()
    marker = tmp_path / "candidate-import.json"
    runner = ROOT / "src" / "hcuopt" / "runtime_probes" / "sglang_overlay_runner.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--phase",
            "candidate",
            "--smoke-runner",
            str(smoke_runner),
            "--spec",
            str(spec),
            "--evidence-dir",
            str(tmp_path / "evidence"),
            "--replacement-point",
            str(candidate),
            "--activation-marker",
            "candidate-v1",
            "--candidate-import-marker",
            str(marker),
            "--expected-artifact-hash",
            candidate_hash,
        ],
        env={
            **os.environ,
            "HCUOPT_TEST_IMPORT_MARKER": str(marker),
            "HCUOPT_TEST_REPLACEMENT": str(candidate),
        },
        check=True,
        capture_output=True,
        text=True,
    )

    observation = json.loads(completed.stdout)
    assert observation["activation_marker"] == "candidate-v1"
    assert observation["loaded_artifact_hash"] == candidate_hash
    assert observation["implementation_hash"] == candidate_hash


def test_overlay_fails_closed_when_cleanup_health_is_bad(tmp_path: Path) -> None:
    configuration, artifact_hash = _overlay_fixture(tmp_path)
    executor = ScriptedExecutor(_overlay_observations(artifact_hash))
    with pytest.raises(ExecutionSafetyError, match="quarantined"):
        OverlayCapabilityProbe(executor, ScriptedCleaner([True, False, True])).run(
            configuration,
            target=TARGET,
            output_dir=tmp_path / "results",
            resource_id="hcu-7",
            fencing_token=9,
        )


def test_evidence_is_atomic_immutable_and_content_addressed(tmp_path: Path) -> None:
    stage0_run_id = str(uuid4())
    uri, digest = write_immutable_json(
        tmp_path,
        stage0_run_id=stage0_run_id,
        probe_type="profiler",
        payload={"capability": "degraded"},
    )
    path = Path(uri.removeprefix("file://"))

    assert path.is_file()
    assert digest == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    assert path.stat().st_mode & 0o222 == 0
    assert write_immutable_json(
        tmp_path,
        stage0_run_id=stage0_run_id,
        probe_type="profiler",
        payload={"capability": "degraded"},
    ) == (uri, digest)
    other_uri, other_digest = write_immutable_json(
        tmp_path,
        stage0_run_id=stage0_run_id,
        probe_type="profiler",
        payload={"capability": "full"},
    )
    assert other_uri != uri
    assert other_digest != digest
    assert path.read_bytes() != Path(other_uri.removeprefix("file://")).read_bytes()
