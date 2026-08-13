import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dcuopt.domain.enums import GateResult, HotPatchCapability, ProfilerCapability
from dcuopt.domain.models import Stage0Evidence
from dcuopt.stage0 import evaluate_stage0


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
    parser = argparse.ArgumentParser(prog="dcuopt")
    sub = parser.add_subparsers(dest="command", required=True)
    stage0 = sub.add_parser("stage0-evaluate", help="evaluate Stage 0 probe evidence")
    stage0.add_argument("evidence", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "stage0-evaluate":
        report = evaluate_stage0(_load_stage0(args.evidence))
        payload = asdict(report)
        payload["mode"] = report.mode.value
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2 if report.mode.value == "stopped_measurement" else 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
