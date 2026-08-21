import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from hcuopt.domain.enums import (
    GateResult,
    HotPatchCapability,
    ProfilerCapability,
    WorkerType,
)
from hcuopt.domain.models import Stage0Evidence
from hcuopt.stage0 import evaluate_stage0


def _load_stage0(path: Path) -> Stage0Evidence:
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return Stage0Evidence(
        measurement=GateResult(raw["measurement"]),
        profiler=ProfilerCapability(raw["profiler"]),
        hot_patch=HotPatchCapability(raw["hot_patch"]),
        hardware_fingerprint=raw["hardware_fingerprint"],
        software_fingerprint=raw["software_fingerprint"],
        timer_resolution_ns=raw.get("timer_resolution_ns"),
        noise_sigma_ns=raw.get("noise_sigma_ns"),
        noise_cv=raw.get("noise_cv"),
        mde_ratio=raw.get("mde_ratio"),
        evidence_uri=raw.get("evidence_uri"),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hcuopt")
    sub = parser.add_subparsers(dest="command", required=True)
    stage0 = sub.add_parser("stage0-evaluate", help="evaluate Stage 0 probe evidence")
    stage0.add_argument("evidence", type=Path)
    api = sub.add_parser("api", help="run the FastAPI control plane")
    api.add_argument("--host", default="0.0.0.0")
    api.add_argument("--port", type=int, default=8000)
    sub.add_parser("db-migrate", help="apply PostgreSQL schema migrations")
    target = sub.add_parser("target-validate", help="validate and normalize a target lock")
    target.add_argument("target", type=Path)
    worker = sub.add_parser("worker", help="run one Agent, Build, or GPU worker")
    worker.add_argument("--id", required=True, dest="worker_id")
    worker.add_argument("--type", required=True, choices=[item.value for item in WorkerType])
    worker.add_argument("--api-url", default=os.getenv("HCUOPT_API_URL", "http://localhost:8000"))
    worker.add_argument("--resource-id")
    worker.add_argument(
        "--adapter-profile",
        default="fake-v1-control-flow-only",
        choices=(
            "fake-v1-control-flow-only",
            "nmz36-framework-smoke-v1",
            "nmz36-stage0-v2",
        ),
    )
    worker.add_argument("--target-lock", type=Path)
    worker.add_argument("--source-root", type=Path)
    worker.add_argument("--stage0-runtime-profile", type=Path)
    worker.add_argument("--evidence-root", type=Path)
    worker.add_argument("--output-dir", type=Path, default=Path("results/worker"))
    demo = sub.add_parser("walking-demo", help="run the fake end-to-end control-flow demo")
    demo.add_argument("--api-url", default=os.getenv("HCUOPT_API_URL", "http://localhost:8000"))
    demo.add_argument("--timeout", type=float, default=60.0)
    demo.add_argument("--external-workers", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "stage0-evaluate":
        report = evaluate_stage0(_load_stage0(args.evidence))
        payload = asdict(report)
        payload["mode"] = report.mode.value
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2 if report.mode.value == "stopped_measurement" else 0
    if args.command == "api":
        import uvicorn

        uvicorn.run("hcuopt.api.app:app", host=args.host, port=args.port)
        return 0
    if args.command == "db-migrate":
        from hcuopt.storage.repository import PostgresRepository

        database_url = os.getenv(
            "HCUOPT_DATABASE_URL", "postgresql://hcuopt:hcuopt@localhost:5432/hcuopt"
        )
        PostgresRepository(database_url).migrate()
        print("PostgreSQL schema is at the latest packaged version")
        return 0
    if args.command == "target-validate":
        from hcuopt.domain.errors import TargetConfigError
        from hcuopt.targets import load_target

        try:
            target = load_target(args.target)
        except TargetConfigError as exc:
            print(f"target validation failed: {exc}", file=sys.stderr)
            return 2
        print(target.model_dump_json(indent=2))
        return 0
    if args.command == "worker":
        from hcuopt.adapters.real_profile import (
            build_nmz36_framework_smoke_registry,
            build_nmz36_stage0_registry,
        )
        from hcuopt.adapters.registry import AdapterRegistry
        from hcuopt.runtime_probes import RuntimeProbeProfile
        from hcuopt.targets import load_target
        from hcuopt.workers.sdk import Worker

        capabilities = {}
        if args.resource_id:
            capabilities["resource_id"] = args.resource_id
        if args.adapter_profile in {
            "nmz36-framework-smoke-v1",
            "nmz36-stage0-v2",
        }:
            if args.target_lock is None:
                print("real adapter profile requires --target-lock", file=sys.stderr)
                return 2
            target = load_target(args.target_lock)
        if args.adapter_profile == "nmz36-framework-smoke-v1":
            adapters = build_nmz36_framework_smoke_registry(
                target,
                args.output_dir,
            )
        elif args.adapter_profile == "nmz36-stage0-v2":
            evidence_root = args.evidence_root
            if evidence_root is None:
                raw_evidence_root = os.getenv("HCUOPT_STAGE0_EVIDENCE_ROOT")
                evidence_root = Path(raw_evidence_root) if raw_evidence_root else None
            missing = [
                name
                for name, value in (
                    ("--source-root", args.source_root),
                    ("--stage0-runtime-profile", args.stage0_runtime_profile),
                    ("--evidence-root", evidence_root),
                )
                if value is None
            ]
            if missing:
                print(
                    "nmz36-stage0-v2 requires " + ", ".join(missing),
                    file=sys.stderr,
                )
                return 2
            assert args.source_root is not None
            assert args.stage0_runtime_profile is not None
            assert evidence_root is not None
            configuration = RuntimeProbeProfile.model_validate_json(
                args.stage0_runtime_profile.read_text(encoding="utf-8")
            )
            adapters = build_nmz36_stage0_registry(
                target,
                args.output_dir,
                source_root=args.source_root,
                runtime_configuration=configuration,
                evidence_root=evidence_root,
            )
        else:
            adapters = AdapterRegistry.fake()
        worker = Worker(
            args.worker_id,
            WorkerType(args.type),
            args.api_url,
            capabilities=capabilities,
            adapters=adapters,
            output_dir=args.output_dir,
        )
        try:
            worker.run_forever()
        except KeyboardInterrupt:
            worker.stop()
        return 0
    if args.command == "walking-demo":
        from hcuopt.demo import run_walking_demo

        summary = run_walking_demo(
            args.api_url,
            timeout_seconds=args.timeout,
            embedded_workers=not args.external_workers,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
