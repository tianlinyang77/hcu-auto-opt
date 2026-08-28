# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx

from hcuopt.contracts.operator_v1 import (
    OperatorHotspotView,
    OperatorProfileDescriptor,
    OperatorProfileRef,
    OperatorRoundPlanSpec,
    OperatorRoundReport,
    OperatorRoundStartRequest,
    OperatorRoundSummary,
    OperatorRunMetrics,
    OperatorServiceIdentity,
    OperatorStartView,
    OperatorWorkloadView,
    RoundPlanPreviewRequest,
    RoundPlanPreviewView,
)


class OperatorCliError(RuntimeError):
    pass


class OperatorHttpClient:
    def __init__(
        self,
        api_url: str,
        *,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._owned = client is None
        self.client = client or httpx.Client(
            base_url=api_url.rstrip("/"), timeout=timeout
        )

    def close(self) -> None:
        if self._owned:
            self.client.close()

    def __enter__(self) -> OperatorHttpClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = self.client.request(method, path, json=payload)
        except httpx.HTTPError as error:
            raise OperatorCliError(f"Operator API is unavailable: {error}") from error
        if response.is_error:
            try:
                detail = response.json()
            except ValueError:
                detail = {}
            code = detail.get("code", f"http_{response.status_code}")
            message = detail.get("message", "Operator API request failed")
            retryable = detail.get("retryable", False)
            raise OperatorCliError(
                f"{code}: {message} (retryable={str(bool(retryable)).lower()})"
            )
        return response.json()

    def identity(self) -> OperatorServiceIdentity:
        return OperatorServiceIdentity.model_validate(
            self._request("GET", "/v1/operator/identity")
        )

    def profiles(self, profile_kind: str | None = None) -> list[OperatorProfileDescriptor]:
        suffix = f"?profile_kind={profile_kind}" if profile_kind else ""
        return [
            OperatorProfileDescriptor.model_validate(item)
            for item in self._request("GET", f"/v1/operator/profiles{suffix}")
        ]

    def profile(
        self,
        profile_kind: str,
        profile_id: str,
        profile_version: int,
    ) -> OperatorProfileDescriptor:
        return OperatorProfileDescriptor.model_validate(
            self._request(
                "GET",
                "/v1/operator/profiles/"
                f"{profile_kind}/{profile_id}/versions/{profile_version}",
            )
        )

    def workloads(self) -> list[OperatorWorkloadView]:
        return [
            OperatorWorkloadView.model_validate(item)
            for item in self._request("GET", "/v1/operator/workloads")
        ]

    def workload(self, profile_id: str, profile_version: int) -> OperatorWorkloadView:
        return OperatorWorkloadView.model_validate(
            self._request(
                "GET",
                f"/v1/operator/workloads/{profile_id}/versions/{profile_version}",
            )
        )

    def hotspots(
        self,
        *,
        target_profile_id: str,
        target_profile_version: int,
        workload_profile_id: str,
        workload_profile_version: int,
    ) -> list[OperatorHotspotView]:
        query = (
            f"target_profile_id={target_profile_id}"
            f"&target_profile_version={target_profile_version}"
            f"&workload_profile_id={workload_profile_id}"
            f"&workload_profile_version={workload_profile_version}"
        )
        return [
            OperatorHotspotView.model_validate(item)
            for item in self._request("GET", f"/v1/operator/hotspots?{query}")
        ]

    def draft(
        self,
        *,
        name: str,
        target_profile_id: str,
        target_profile_version: int,
        workload_profile_id: str,
        workload_profile_version: int,
        measurement_profile_id: str,
        measurement_profile_version: int,
        hotspot_index: int,
        candidate_indexes: tuple[int, ...],
        max_promoted: int,
        idempotency_key: str | None,
    ) -> OperatorRoundPlanSpec:
        self.workload(workload_profile_id, workload_profile_version)
        self.profile(
            "measurement", measurement_profile_id, measurement_profile_version
        )
        available = self.hotspots(
            target_profile_id=target_profile_id,
            target_profile_version=target_profile_version,
            workload_profile_id=workload_profile_id,
            workload_profile_version=workload_profile_version,
        )
        if hotspot_index < 1 or hotspot_index > len(available):
            raise OperatorCliError(
                f"Hotspot index {hotspot_index} is unavailable; run hotspot list first"
            )
        hotspot = available[hotspot_index - 1]
        packages = hotspot.candidate_packages
        selected_indexes = candidate_indexes or tuple(range(1, min(4, len(packages)) + 1))
        if (
            len(selected_indexes) < 2
            or len(selected_indexes) > 4
            or len(set(selected_indexes)) != len(selected_indexes)
            or any(index < 1 or index > len(packages) for index in selected_indexes)
        ):
            raise OperatorCliError(
                "Plan drafting requires 2-4 unique Candidate indexes from hotspot show"
            )
        selected = tuple(packages[index - 1] for index in selected_indexes)
        fingerprint = {
            "name": name,
            "target": [target_profile_id, target_profile_version],
            "workload": [workload_profile_id, workload_profile_version],
            "measurement": [measurement_profile_id, measurement_profile_version],
            "hotspot_id": str(hotspot.hotspot.hotspot_id),
            "candidates": [
                {
                    "candidate_id": str(item.candidate_id),
                    "source_package_ref": item.source_package_ref.model_dump(mode="json"),
                    "optimization_intent": item.suggested_optimization_intent,
                }
                for item in selected
            ],
            "max_promoted": max_promoted,
        }
        digest = hashlib.sha256(
            json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return OperatorRoundPlanSpec(
            name=name,
            target_profile={
                "profile_id": target_profile_id,
                "profile_version": target_profile_version,
            },
            workload_profile={
                "profile_id": workload_profile_id,
                "profile_version": workload_profile_version,
            },
            measurement_profile={
                "profile_id": measurement_profile_id,
                "profile_version": measurement_profile_version,
            },
            hotspot=hotspot.hotspot,
            candidates=tuple(
                {
                    "ordinal": ordinal,
                    "source_package_ref": package.source_package_ref,
                    "optimization_intent": package.suggested_optimization_intent,
                }
                for ordinal, package in enumerate(selected)
            ),
            max_promoted=max_promoted,
            idempotency_key=idempotency_key or f"operator-draft-{digest[:24]}",
        )

    def plan(self, spec: OperatorRoundPlanSpec) -> RoundPlanPreviewView:
        identity = self.identity()
        selected = {
            kind: self.profile(
                kind,
                selector.profile_id,
                selector.profile_version,
            )
            for kind, selector in (
                ("target", spec.target_profile),
                ("workload", spec.workload_profile),
                ("measurement", spec.measurement_profile),
            )
        }
        request = RoundPlanPreviewRequest(
            name=spec.name,
            run_mode="scripted",
            target_profile=_profile_ref(selected["target"]),
            workload_profile=_profile_ref(selected["workload"]),
            measurement_profile=_profile_ref(selected["measurement"]),
            hotspot=spec.hotspot,
            candidates=spec.candidates,
            max_promoted=spec.max_promoted,
            idempotency_key=spec.idempotency_key,
            expected_service_identity=identity.model_dump(mode="json"),
        )
        return RoundPlanPreviewView.model_validate(
            self._request(
                "POST",
                "/v1/operator/round-plans:preview",
                payload=request.model_dump(mode="json"),
            )
        )

    def start(
        self,
        preview: RoundPlanPreviewView,
        *,
        actor: str,
        acknowledge_warnings: bool,
        idempotency_key: str | None = None,
    ) -> OperatorStartView:
        if not preview.start_allowed:
            blocked = ", ".join(
                check.code for check in preview.checks if check.status == "block"
            )
            raise OperatorCliError(f"Preview is blocked: {blocked or 'unknown'}")
        if preview.required_ack_codes and not acknowledge_warnings:
            raise OperatorCliError(
                "Preview warnings require --ack-warnings: "
                + ", ".join(preview.required_ack_codes)
            )
        identity = self.identity()
        request = OperatorRoundStartRequest(
            preview_id=preview.preview_id,
            resolved_plan_hash=preview.resolved_plan_hash,
            actor=actor,
            idempotency_key=(
                idempotency_key or f"operator-start-{preview.preview_id}"
            ),
            acknowledged_warning_codes=(
                preview.required_ack_codes if acknowledge_warnings else ()
            ),
            expected_service_identity=identity.model_dump(mode="json"),
        )
        return OperatorStartView.model_validate(
            self._request(
                "POST",
                f"/v1/operator/round-plans/{preview.preview_id}:start",
                payload=request.model_dump(mode="json"),
            )
        )

    def status(self, round_id: UUID) -> OperatorRoundSummary:
        return OperatorRoundSummary.model_validate(
            self._request(
                "GET", f"/v1/operator/search-rounds/{round_id}/summary"
            )
        )

    def report(self, round_id: UUID) -> OperatorRoundReport:
        return OperatorRoundReport.model_validate(
            self._request(
                "GET", f"/v1/operator/search-rounds/{round_id}/report"
            )
        )


def _profile_ref(profile: OperatorProfileDescriptor) -> OperatorProfileRef:
    return OperatorProfileRef(
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        profile_kind=profile.profile_kind,
        profile_hash=profile.profile_hash,
    )


def configure_operator_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    default_api_url: str,
) -> None:
    profile = subparsers.add_parser("profile", help="list or show Operator Profiles")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)
    profile_list = profile_sub.add_parser("list", help="list immutable Profiles")
    profile_list.add_argument(
        "--kind", choices=("target", "workload", "measurement")
    )
    profile_list.add_argument("--json", action="store_true")
    profile_list.add_argument("--api-url", default=default_api_url)
    profile_show = profile_sub.add_parser("show", help="show one exact Profile")
    profile_show.add_argument(
        "kind", choices=("target", "workload", "measurement")
    )
    profile_show.add_argument("profile_id")
    profile_show.add_argument("--version", type=int, required=True)
    profile_show.add_argument("--json", action="store_true")
    profile_show.add_argument("--api-url", default=default_api_url)

    workload = subparsers.add_parser("workload", help="discover workload Profiles")
    workload_sub = workload.add_subparsers(dest="workload_command", required=True)
    workload_list = workload_sub.add_parser("list", help="list immutable workloads")
    workload_list.add_argument("--json", action="store_true")
    workload_list.add_argument("--api-url", default=default_api_url)
    workload_show = workload_sub.add_parser("show", help="show one exact workload")
    workload_show.add_argument("profile_id")
    workload_show.add_argument("--version", type=int, required=True)
    workload_show.add_argument("--json", action="store_true")
    workload_show.add_argument("--api-url", default=default_api_url)

    hotspot = subparsers.add_parser("hotspot", help="discover trusted Hotspot inputs")
    hotspot_sub = hotspot.add_subparsers(dest="hotspot_command", required=True)
    for command in ("list", "show"):
        selected = hotspot_sub.add_parser(command, help=f"{command} trusted Hotspots")
        if command == "show":
            selected.add_argument("index", type=int)
        selected.add_argument("--target-profile", default="m2-scripted-target")
        selected.add_argument("--target-version", type=int, default=1)
        selected.add_argument("--workload-profile", default="m2-scripted-workload")
        selected.add_argument("--workload-version", type=int, default=1)
        selected.add_argument("--json", action="store_true")
        selected.add_argument("--api-url", default=default_api_url)

    round_parser = subparsers.add_parser("round", help="operate a Scripted Round")
    round_sub = round_parser.add_subparsers(dest="round_command", required=True)
    draft = round_sub.add_parser(
        "draft", help="generate a Plan Spec from trusted discovery inputs"
    )
    draft.add_argument("--name", default="Scripted Operator Preview")
    draft.add_argument("--target-profile", default="m2-scripted-target")
    draft.add_argument("--target-version", type=int, default=1)
    draft.add_argument("--workload-profile", default="m2-scripted-workload")
    draft.add_argument("--workload-version", type=int, default=1)
    draft.add_argument("--measurement-profile", default="m2-scripted-standard")
    draft.add_argument("--measurement-version", type=int, default=1)
    draft.add_argument("--hotspot-index", type=int, default=1)
    draft.add_argument("--candidate-index", action="append", type=int, default=[])
    draft.add_argument("--max-promoted", type=int, default=2)
    draft.add_argument("--idempotency-key")
    draft.add_argument("--output", type=Path, default=Path("operator-plan.json"))
    draft.add_argument("--json", action="store_true")
    draft.add_argument("--api-url", default=default_api_url)
    plan = round_sub.add_parser("plan", help="create an immutable Plan Preview")
    plan.add_argument("spec", type=Path)
    plan.add_argument("--output", type=Path)
    plan.add_argument("--json", action="store_true")
    plan.add_argument("--api-url", default=default_api_url)
    start = round_sub.add_parser("start", help="start a persisted Preview")
    start.add_argument("preview", type=Path)
    start.add_argument("--actor", required=True)
    start.add_argument("--idempotency-key")
    start.add_argument("--ack-warnings", action="store_true")
    start.add_argument("--output", type=Path)
    start.add_argument("--json", action="store_true")
    start.add_argument("--api-url", default=default_api_url)
    status = round_sub.add_parser("status", help="read one Round status")
    status.add_argument("start", type=Path)
    status.add_argument("--output", type=Path)
    status.add_argument("--json", action="store_true")
    status.add_argument("--api-url", default=default_api_url)
    report = round_sub.add_parser("report", help="export an interim or final Report")
    report.add_argument("start", type=Path)
    report.add_argument("--output", type=Path)
    report.add_argument("--json", action="store_true")
    report.add_argument("--api-url", default=default_api_url)
    run = round_sub.add_parser("run", help="plan, start, status, and report in one command")
    run.add_argument("spec", type=Path)
    run.add_argument("--actor", required=True)
    run.add_argument("--ack-warnings", action="store_true")
    run.add_argument("--output-dir", type=Path, default=Path("results/operator"))
    run.add_argument("--json", action="store_true")
    run.add_argument("--api-url", default=default_api_url)


def run_operator_command(args: argparse.Namespace) -> int | None:
    if args.command not in {"profile", "workload", "hotspot", "round"}:
        return None
    try:
        with OperatorHttpClient(args.api_url) as client:
            if args.command == "profile":
                if args.profile_command == "list":
                    profiles = client.profiles(args.kind)
                    _emit(
                        [item.model_dump(mode="json") for item in profiles],
                        json_stdout=args.json,
                        human=_profile_list_text(profiles),
                    )
                else:
                    profile = client.profile(args.kind, args.profile_id, args.version)
                    _emit(
                        profile.model_dump(mode="json"),
                        json_stdout=args.json,
                        human=_profile_text(profile),
                    )
                return 0
            if args.command == "workload":
                if args.workload_command == "list":
                    workloads = client.workloads()
                    _emit(
                        [item.model_dump(mode="json") for item in workloads],
                        json_stdout=args.json,
                        human=_workload_list_text(workloads),
                    )
                else:
                    workload = client.workload(args.profile_id, args.version)
                    _emit(
                        workload.model_dump(mode="json"),
                        json_stdout=args.json,
                        human=_workload_text(workload),
                    )
                return 0
            if args.command == "hotspot":
                hotspots = client.hotspots(
                    target_profile_id=args.target_profile,
                    target_profile_version=args.target_version,
                    workload_profile_id=args.workload_profile,
                    workload_profile_version=args.workload_version,
                )
                if args.hotspot_command == "list":
                    _emit(
                        [item.model_dump(mode="json") for item in hotspots],
                        json_stdout=args.json,
                        human=_hotspot_list_text(hotspots),
                    )
                else:
                    if args.index < 1 or args.index > len(hotspots):
                        raise OperatorCliError(
                            f"Hotspot index {args.index} is unavailable"
                        )
                    selected = hotspots[args.index - 1]
                    _emit(
                        selected.model_dump(mode="json"),
                        json_stdout=args.json,
                        human=_hotspot_text(selected, args.index),
                    )
                return 0
            if args.round_command == "draft":
                spec = client.draft(
                    name=args.name,
                    target_profile_id=args.target_profile,
                    target_profile_version=args.target_version,
                    workload_profile_id=args.workload_profile,
                    workload_profile_version=args.workload_version,
                    measurement_profile_id=args.measurement_profile,
                    measurement_profile_version=args.measurement_version,
                    hotspot_index=args.hotspot_index,
                    candidate_indexes=tuple(args.candidate_index),
                    max_promoted=args.max_promoted,
                    idempotency_key=args.idempotency_key,
                )
                _emit(
                    spec.model_dump(mode="json"),
                    args.output,
                    json_stdout=args.json,
                    human=(
                        f"Plan Spec: {args.output.resolve()}\n"
                        f"Candidates: {len(spec.candidates)}; "
                        f"max promoted: {spec.max_promoted}\n"
                        "Mode: synthetic Scripted; automatic release: false"
                    ),
                )
                return 0
            if args.round_command == "plan":
                preview = client.plan(_load(args.spec, OperatorRoundPlanSpec))
                _emit(
                    preview.model_dump(mode="json"),
                    args.output,
                    json_stdout=args.json,
                    human=_preview_text(preview),
                )
                return 0 if preview.start_allowed else 2
            if args.round_command == "start":
                preview = _load(args.preview, RoundPlanPreviewView)
                started = client.start(
                    preview,
                    actor=args.actor,
                    acknowledge_warnings=args.ack_warnings,
                    idempotency_key=args.idempotency_key,
                )
                _emit(
                    started.model_dump(mode="json"),
                    args.output,
                    json_stdout=args.json,
                    human=_start_text(started),
                )
                return 0 if started.executable else 2
            if args.round_command in {"status", "report"}:
                started = _load(args.start, OperatorStartView)
                result = (
                    client.status(started.round_id)
                    if args.round_command == "status"
                    else client.report(started.round_id)
                )
                _emit(
                    result.model_dump(mode="json"),
                    args.output,
                    json_stdout=args.json,
                    human=(
                        _summary_text(result)
                        if isinstance(result, OperatorRoundSummary)
                        else _report_text(result)
                    ),
                )
                return 0
            if args.round_command == "run":
                run_started_at = datetime.now(timezone.utc)
                run_started = time.perf_counter()
                preview = client.plan(_load(args.spec, OperatorRoundPlanSpec))
                _prepare_round_run_output_dir(args.output_dir, preview)
                _write_json(
                    preview.model_dump(mode="json"), args.output_dir / "preview.json"
                )
                metrics_written = False
                acknowledged_warning_count = (
                    len(preview.required_ack_codes) if args.ack_warnings else 0
                )
                report_started: float | None = None
                try:
                    if not preview.start_allowed:
                        _write_run_metrics(
                            args.output_dir,
                            outcome="blocked",
                            started_at=run_started_at,
                            started=run_started,
                            manual_intervention_count=0,
                            preflight_blocked_before_hcu_count=1,
                            report_generation_seconds=0.0,
                            error_code="operator_preview_blocked",
                        )
                        metrics_written = True
                        blocked = ", ".join(
                            item.code
                            for item in preview.checks
                            if item.status == "block"
                        )
                        raise OperatorCliError(
                            f"Preview is blocked: {blocked or 'unknown'}"
                        )
                    started = client.start(
                        preview,
                        actor=args.actor,
                        acknowledge_warnings=args.ack_warnings,
                    )
                    _write_json(
                        started.model_dump(mode="json"),
                        args.output_dir / "start.json",
                    )
                    if not started.executable:
                        _write_run_metrics(
                            args.output_dir,
                            outcome="failed",
                            started_at=run_started_at,
                            started=run_started,
                            manual_intervention_count=acknowledged_warning_count,
                            preflight_blocked_before_hcu_count=0,
                            report_generation_seconds=0.0,
                            error_code=started.error_code or "operator_start_failed",
                        )
                        metrics_written = True
                        raise OperatorCliError(
                            "StartIntent did not finalize: "
                            f"state={started.state}; "
                            f"error_code={started.error_code or 'none'}"
                        )
                    summary = client.status(started.round_id)
                    _write_json(
                        summary.model_dump(mode="json"),
                        args.output_dir / "summary.json",
                    )
                    report_started = time.perf_counter()
                    report = client.report(started.round_id)
                    report_seconds = time.perf_counter() - report_started
                    _write_json(
                        report.model_dump(mode="json"),
                        args.output_dir / "report.json",
                    )
                    _write_run_metrics(
                        args.output_dir,
                        outcome="succeeded",
                        started_at=run_started_at,
                        started=run_started,
                        manual_intervention_count=acknowledged_warning_count,
                        preflight_blocked_before_hcu_count=0,
                        report_generation_seconds=report_seconds,
                        error_code=None,
                    )
                    metrics_written = True
                except (OSError, ValueError, OperatorCliError):
                    if not metrics_written:
                        report_seconds = (
                            0.0
                            if report_started is None
                            else time.perf_counter() - report_started
                        )
                        _write_run_metrics(
                            args.output_dir,
                            outcome="failed",
                            started_at=run_started_at,
                            started=run_started,
                            manual_intervention_count=acknowledged_warning_count,
                            preflight_blocked_before_hcu_count=0,
                            report_generation_seconds=report_seconds,
                            error_code="operator_run_failed",
                        )
                    raise
                _emit(
                    {
                        "synthetic": True,
                        "round_id": str(started.round_id),
                        "state": summary.state.value,
                        "next_action": summary.next_action,
                        "report_status": report.report_status,
                        "output_dir": str(args.output_dir.resolve()),
                        "automatic_release_allowed": False,
                    },
                    json_stdout=args.json,
                    human=(
                        _summary_text(summary)
                        + f"\nReport: {report.report_status}; files: {args.output_dir.resolve()}"
                    ),
                )
                return 0
    except (OSError, ValueError, OperatorCliError) as error:
        print(f"operator command failed: {error}", file=sys.stderr)
        return 2
    return 1


def _load(path: Path, model: type[Any]) -> Any:
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def _emit(
    value: Any,
    output: Path | None = None,
    *,
    json_stdout: bool = False,
    human: str | None = None,
) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if output is not None:
        _write_json(value, output)
    print(encoded if json_stdout or human is None else human + "\n", end="")


def _write_json(value: Any, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def _write_run_metrics(
    output_dir: Path,
    *,
    outcome: str,
    started_at: datetime,
    started: float,
    manual_intervention_count: int,
    preflight_blocked_before_hcu_count: int,
    report_generation_seconds: float,
    error_code: str | None,
) -> OperatorRunMetrics:
    metrics = OperatorRunMetrics(
        outcome=outcome,
        started_at=started_at,
        completed_at=datetime.now(timezone.utc),
        operator_active_seconds=time.perf_counter() - started,
        manual_intervention_count=manual_intervention_count,
        preflight_blocked_before_hcu_count=preflight_blocked_before_hcu_count,
        report_generation_seconds=report_generation_seconds,
        error_code=error_code,
    )
    _write_json(metrics.model_dump(mode="json"), output_dir / "metrics.json")
    return metrics


def _prepare_round_run_output_dir(
    output_dir: Path,
    preview: RoundPlanPreviewView,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    preview_path = output_dir / "preview.json"
    owned_paths = tuple(
        output_dir / name
        for name in ("start.json", "summary.json", "report.json", "metrics.json")
    )
    if not preview_path.exists():
        if any(path.exists() for path in owned_paths):
            raise OperatorCliError(
                "Operator output directory contains unbound handoff files; "
                "choose a clean directory"
            )
        return
    existing = _load(preview_path, RoundPlanPreviewView)
    if (
        existing.preview_id != preview.preview_id
        or existing.resolved_plan_hash != preview.resolved_plan_hash
    ):
        raise OperatorCliError(
            "Operator output directory belongs to another Plan Preview; "
            "choose a different directory"
        )


def _profile_list_text(profiles: list[OperatorProfileDescriptor]) -> str:
    if not profiles:
        return "No Operator Profiles matched."
    return "\n".join(
        f"{item.profile_kind:11} {item.profile_id}@{item.profile_version} "
        f"state={item.state} synthetic={str(item.synthetic).lower()}"
        for item in profiles
    )


def _profile_text(profile: OperatorProfileDescriptor) -> str:
    return (
        f"{profile.display_name}\n"
        f"Profile: {profile.profile_kind}/{profile.profile_id}@{profile.profile_version}\n"
        f"State: {profile.state}; synthetic={str(profile.synthetic).lower()}\n"
        f"Summary: {profile.summary}"
    )


def _workload_list_text(workloads: list[OperatorWorkloadView]) -> str:
    if not workloads:
        return "No Operator Workloads matched."
    return "\n".join(
        f"[{index}] {item.profile.profile_id}@{item.profile.profile_version} "
        f"workload={item.authority_refs.workload_id} state={item.state}"
        for index, item in enumerate(workloads, start=1)
    )


def _workload_text(workload: OperatorWorkloadView) -> str:
    refs = workload.authority_refs
    return (
        f"{workload.display_name}\n"
        f"Profile: {workload.profile.profile_id}@{workload.profile.profile_version}\n"
        f"Workload: {refs.workload_id}; Hash: {refs.workload_hash}\n"
        f"Hotspot scope: {refs.hotspot_scope_id}\n"
        "Mode: synthetic Scripted"
    )


def _hotspot_list_text(hotspots: list[OperatorHotspotView]) -> str:
    if not hotspots:
        return "No trusted Operator Hotspots matched."
    return "\n".join(
        f"[{index}] {item.symbol} replacement={item.hotspot.replacement_point} "
        f"candidates={len(item.candidate_packages)} source={item.hotspot.source}"
        for index, item in enumerate(hotspots, start=1)
    )


def _hotspot_text(hotspot: OperatorHotspotView, index: int) -> str:
    packages = "\n".join(
        f"  [{candidate_index}] {item.candidate_id} path={item.replacement_path}"
        for candidate_index, item in enumerate(hotspot.candidate_packages, start=1)
    )
    return (
        f"Hotspot [{index}]: {hotspot.symbol}\n"
        f"Replacement: {hotspot.hotspot.replacement_point}\n"
        f"Shape/dtype: {list(hotspot.hotspot.shape)} / {hotspot.hotspot.dtype}\n"
        f"Source: {hotspot.hotspot.source}; candidates: "
        f"{len(hotspot.candidate_packages)}\n"
        f"{packages or '  none'}"
    )


def _preview_text(preview: RoundPlanPreviewView) -> str:
    blocks = ", ".join(item.code for item in preview.checks if item.status == "block")
    warnings = ", ".join(preview.required_ack_codes)
    return (
        f"Plan Preview: {preview.preview_id}\n"
        f"Start allowed: {str(preview.start_allowed).lower()}; expires: {preview.expires_at}\n"
        f"Blocks: {blocks or 'none'}; warnings: {warnings or 'none'}\n"
        "Mode: synthetic Scripted; automatic release: false"
    )


def _start_text(started: OperatorStartView) -> str:
    return (
        f"StartIntent: {started.intent_id}\n"
        f"Round: {started.round_id}; state: {started.state}; "
        f"replayed={str(started.replayed).lower()}\n"
        f"Executable by next control-plane stage: {str(started.executable).lower()}\n"
        "Mode: synthetic Scripted; automatic release: false"
    )


def _summary_text(summary: OperatorRoundSummary) -> str:
    return (
        f"Round: {summary.round_id}; state: {summary.state.value}\n"
        f"Next action: {summary.next_action} — {summary.reason}\n"
        f"Candidates: {summary.candidate_count}; build terminals: "
        f"{summary.build_terminal_count}; evidence: {summary.evidence_status}\n"
        "Mode: synthetic Scripted; automatic release: false"
    )


def _report_text(report: OperatorRoundReport) -> str:
    return (
        f"Report: {report.report_status}; Round: {report.summary.round_id}\n"
        f"State: {report.summary.state.value}; evidence: "
        f"{report.summary.evidence_status}\n"
        f"Conclusion boundary: {report.conclusion_boundary}\n"
        "Formal signoff: disabled; automatic release: false"
    )
