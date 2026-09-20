import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from hcuopt.agent.cli import (
    configure_agent_generation_parsers,
    run_agent_generation_command,
)
from hcuopt.domain.enums import (
    GateResult,
    HotPatchCapability,
    ProfilerCapability,
    WorkerType,
)
from hcuopt.domain.models import Stage0Evidence
from hcuopt.operator.cli import configure_operator_parsers, run_operator_command
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
    viewer = sub.add_parser(
        "framework-viewer", help="manage a local result viewer (read-only default)"
    )
    viewer_sub = viewer.add_subparsers(dest="viewer_action", required=True)
    viewer_serve = viewer_sub.add_parser(
        "serve", help="run in foreground; Ctrl+C stops this instance"
    )
    viewer_serve.add_argument("config", type=Path)
    viewer_serve.add_argument("--dsn-env", default="HCUOPT_VIEWER_DATABASE_URL")
    viewer_serve.add_argument("--signing-dsn-env", help="explicit independent signing DSN variable")
    for action in ("status", "stop", "revoke-signing"):
        viewer_control = viewer_sub.add_parser(action)
        viewer_control.add_argument("instance_directory", type=Path)
    evidence = sub.add_parser(
        "bw20-evidence-verify", help="replay historical BW20 F1 evidence without HCU or DB access"
    )
    evidence.add_argument("directory", type=Path)
    evidence.add_argument("--receipt-sha256", required=True,
                          help="trusted out-of-band SHA256 of independent-readback.json")
    stage0 = sub.add_parser("stage0-evaluate", help="evaluate Stage 0 probe evidence")
    stage0.add_argument("evidence", type=Path)
    api = sub.add_parser("api", help="run the FastAPI control plane")
    api.add_argument("--host", default="0.0.0.0")
    api.add_argument("--port", type=int, default=8000)
    sub.add_parser("db-migrate", help="apply PostgreSQL schema migrations")
    target = sub.add_parser("target-validate", help="validate and normalize a target lock")
    target.add_argument("target", type=Path)
    readiness = sub.add_parser(
        "formal-readiness",
        help="verify one no-HCU M2a Formal readiness manifest",
    )
    readiness.add_argument("manifest", type=Path)
    readiness.add_argument(
        "--repository-root",
        type=Path,
        default=Path("."),
        help="repository root used to verify immutable evidence paths",
    )
    readiness.add_argument("--json", action="store_true")
    endpoint_adjudication = sub.add_parser(
        "endpoint-adjudicate",
        help="independently reread one frozen endpoint campaign and publish a D verdict",
    )
    endpoint_adjudication.add_argument("request", type=Path)
    endpoint_adjudication.add_argument(
        "--allow-root",
        action="append",
        type=Path,
        required=True,
        help="authorized local evidence root; may be repeated",
    )
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
    configure_operator_parsers(
        sub,
        default_api_url=os.getenv("HCUOPT_API_URL", "http://localhost:8000"),
    )
    configure_agent_generation_parsers(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "framework-viewer":
        from hcuopt.deployment.viewer_service import (
            ViewerServiceError,
            control_instance,
            load_config,
            serve,
        )

        try:
            if args.viewer_action == "serve":
                if args.signing_dsn_env == args.dsn_env:
                    raise ViewerServiceError("signing_requires_separate_dsn_variable")
                dsn = os.environ.pop(args.dsn_env, "")
                if args.signing_dsn_env:
                    signing_dsn = os.environ.pop(args.signing_dsn_env, "")
                    return serve(load_config(args.config), dsn, signing_database_dsn=signing_dsn)
                return serve(load_config(args.config), dsn)
            print(json.dumps(control_instance(args.instance_directory, args.viewer_action)))
            return 0
        except ViewerServiceError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 2
        except (OSError, ValueError):
            print(json.dumps({"ok": False, "error": "viewer_command_failed"}))
            return 2
    if args.command == "bw20-evidence-verify":
        from hcuopt.deployment.bw20_evidence import (
            EvidenceVerificationError,
            verify_bw20_evidence,
        )

        try:
            report = verify_bw20_evidence(args.directory, receipt_sha256=args.receipt_sha256)
        except EvidenceVerificationError as exc:
            print(json.dumps({"historical_evidence_verified": False, "error": str(exc)}))
            return 2
        print(json.dumps(report, indent=2))
        return 0
    agent_generation_result = run_agent_generation_command(args)
    if agent_generation_result is not None:
        return agent_generation_result
    operator_result = run_operator_command(args)
    if operator_result is not None:
        return operator_result
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
    if args.command == "formal-readiness":
        from hcuopt.operator.readiness import (
            FormalReadinessAuditor,
            FormalReadinessError,
            load_formal_readiness_manifest,
        )

        try:
            manifest = load_formal_readiness_manifest(args.manifest)
            report = FormalReadinessAuditor().evaluate(
                manifest,
                args.repository_root,
            )
        except (OSError, FormalReadinessError, ValueError) as exc:
            print(f"formal readiness failed: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(report.model_dump_json(indent=2))
        else:
            print(
                "\n".join(
                    (
                        f"M2a Formal readiness: {report.decision.upper()}",
                        f"Audit: {report.audit_id}",
                        f"Verified repository evidence: {report.verified_evidence_count}",
                        "Blockers: "
                        + (", ".join(report.blocker_codes) or "none"),
                        "HCU accessed: false; Formal Round created: false",
                        "Profile registration: disabled; automatic release: false",
                    )
                )
            )
        return 0 if report.decision == "ready_for_window_authorization" else 2
    if args.command == "endpoint-adjudicate":
        from hcuopt.contracts.endpoint_adjudication_v1 import (
            EndpointFormalAdjudicationRequest,
        )
        from hcuopt.evaluation.endpoint_adjudication import adjudicate_endpoint_campaign

        try:
            request = EndpointFormalAdjudicationRequest.model_validate_json(
                args.request.read_text(encoding="utf-8")
            )
            result = adjudicate_endpoint_campaign(
                request,
                allowed_roots=tuple(args.allow_root),
            )
        except (OSError, ValueError) as exc:
            print(json.dumps({"verdict": "invalid", "error": str(exc)}))
            return 2
        print(result.model_dump_json(indent=2))
        return 2 if result.verdict == "invalid" else 0
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
