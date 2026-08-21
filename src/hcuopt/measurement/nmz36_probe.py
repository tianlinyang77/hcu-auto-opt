"""Run the five S0-B Formal probes on the locked nmz36 deployment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from hcuopt.adapters.profiles import REAL_STAGE0_PROFILE
from hcuopt.adapters.resource_cleaner import ContainerResourceCleaner
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.domain.enums import Stage0ProbeType
from hcuopt.measurement.harness import EvidenceMeasurementHarness
from hcuopt.measurement.nmz36_runtime import (
    DockerProcessLifecycleRecorder,
    HySmiTelemetryCollector,
    ManagedProcessRegistry,
    Nmz36WorkloadFactory,
)
from hcuopt.measurement.stage0 import Stage0MeasurementProbeAdapter
from hcuopt.targets import load_target, target_fingerprint


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-lock", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--task-id", required=True, type=UUID)
    parser.add_argument("--stage0-run-id", required=True, type=UUID)
    parser.add_argument("--target-snapshot-id", required=True, type=UUID)
    parser.add_argument("--lease-id", required=True, type=UUID)
    parser.add_argument("--fencing-token", required=True, type=int)
    parser.add_argument("--workload-id", default="stage0-short-kernel-v1")
    parser.add_argument(
        "--probe",
        action="append",
        choices=("fingerprint", "timer", "noise", "known_signal", "null_signal"),
        help="run only the selected probe; repeat to select more than one",
    )
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    target = load_target(args.target_lock)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source_root = args.source_root.resolve(strict=True)
    if args.fencing_token < 1:
        raise ValueError("fencing token must be positive")
    results: dict[str, Any] = {}
    all_probe_types = (
        Stage0ProbeType.FINGERPRINT,
        Stage0ProbeType.TIMER,
        Stage0ProbeType.NOISE,
        Stage0ProbeType.KNOWN_SIGNAL,
        Stage0ProbeType.NULL_SIGNAL,
    )
    probe_types = (
        tuple(Stage0ProbeType(value) for value in args.probe)
        if args.probe
        else all_probe_types
    )
    for probe_type in probe_types:
        registry = ManagedProcessRegistry()
        factory = Nmz36WorkloadFactory(
            target=target,
            source_root=source_root,
            output_dir=output_dir,
            resource_id="hcu-7",
            fencing_token=args.fencing_token,
            registry=registry,
        )
        timer = factory.timer()
        cleaner = ContainerResourceCleaner(target, profile=REAL_STAGE0_PROFILE)
        provenance = AdapterProvenance(
            profile=REAL_STAGE0_PROFILE,
            capability="measurement_harness",
            adapter_name="Nmz36DockerEvidenceMeasurementHarness",
            adapter_version="1",
            implementation_kind="real",
        )
        harness = EvidenceMeasurementHarness(
            provenance=provenance,
            stable_identity=target.model_dump(mode="json"),
            workload_factory=lambda _restart: (_ for _ in ()).throw(
                RuntimeError("legacy measurement path is disabled for Formal nmz36")
            ),
            telemetry=HySmiTelemetryCollector(target, registry),
            device_timer=timer,
            synchronize=timer.synchronize,
            cleaner=cleaner,
            formal_workload_factory=factory.workload,
            lifecycle_recorder=DockerProcessLifecycleRecorder(),
        )
        adapter = Stage0MeasurementProbeAdapter(
            harness,
            target,
            measurement_plan_factory=lambda _probe, _payload: {},
            known_signal_detector=lambda _run, _payload: (_ for _ in ()).throw(
                RuntimeError("Formal verdicts belong to D")
            ),
            null_signal_detector=lambda _run, _payload: (_ for _ in ()).throw(
                RuntimeError("Formal verdicts belong to D")
            ),
        )
        payload = {
            "task_id": str(args.task_id),
            "stage0_run_id": str(args.stage0_run_id),
            "target_snapshot_id": str(args.target_snapshot_id),
            "target_fingerprint": target_fingerprint(target),
            "target": target.model_dump(mode="json"),
            "workload_id": args.workload_id,
            "adapter_profile": REAL_STAGE0_PROFILE,
            "probe_type": probe_type.value,
            "protocol_version": "s0-g0-v1",
            "mode": "formal",
            "_job_context": {
                "lease_id": str(args.lease_id),
                "lease_scope": "exclusive",
                "resource_id": "hcu-7",
                "fencing_token": args.fencing_token,
            },
        }
        try:
            result = adapter.run_probe(payload, output_dir)
        finally:
            timer.close()
        results[probe_type.value] = {
            "summary": result.summary,
            "raw_evidence_uri": result.raw_evidence_uri,
            "raw_evidence_hash": result.raw_evidence_hash,
            "cleanup_evidence": result.cleanup_evidence,
            "synthetic": result.synthetic,
            "adapter_provenance": [
                item.model_dump(mode="json") for item in result.adapter_provenance
            ],
        }
        _write_json(output_dir / "measurement-probe-references.json", results)
    return results


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run(args)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
