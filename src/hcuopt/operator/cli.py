# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx

from hcuopt.contracts.operator_v1 import (
    OperatorProfileDescriptor,
    OperatorProfileRef,
    OperatorRoundPlanSpec,
    OperatorRoundReport,
    OperatorRoundStartRequest,
    OperatorRoundSummary,
    OperatorServiceIdentity,
    OperatorStartView,
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

    round_parser = subparsers.add_parser("round", help="operate a Scripted Round")
    round_sub = round_parser.add_subparsers(dest="round_command", required=True)
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
    if args.command not in {"profile", "round"}:
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
                preview = client.plan(_load(args.spec, OperatorRoundPlanSpec))
                _prepare_round_run_output_dir(args.output_dir, preview)
                _write_json(
                    preview.model_dump(mode="json"), args.output_dir / "preview.json"
                )
                started = client.start(
                    preview,
                    actor=args.actor,
                    acknowledge_warnings=args.ack_warnings,
                )
                _write_json(
                    started.model_dump(mode="json"), args.output_dir / "start.json"
                )
                if not started.executable:
                    raise OperatorCliError(
                        "StartIntent did not finalize: "
                        f"state={started.state}; "
                        f"error_code={started.error_code or 'none'}"
                    )
                summary = client.status(started.round_id)
                _write_json(
                    summary.model_dump(mode="json"), args.output_dir / "summary.json"
                )
                report = client.report(started.round_id)
                _write_json(
                    report.model_dump(mode="json"), args.output_dir / "report.json"
                )
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


def _prepare_round_run_output_dir(
    output_dir: Path,
    preview: RoundPlanPreviewView,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    preview_path = output_dir / "preview.json"
    owned_paths = tuple(
        output_dir / name
        for name in ("start.json", "summary.json", "report.json")
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
