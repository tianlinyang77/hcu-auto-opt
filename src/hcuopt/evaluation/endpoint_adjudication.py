# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Independent, fail-closed D adjudication for a frozen endpoint campaign."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from hcuopt.contracts.endpoint_adjudication_v1 import (
    EndpointAdjudicationGroupResult,
    EndpointFormalAdjudicationRequest,
    EndpointFormalAdjudicationResult,
)
from hcuopt.measurement.models import ProcessLifecycleRecordV2

_T_95_DF7 = 2.364624251


class EndpointEvidenceError(ValueError):
    """Stable failure raised when independent evidence reread cannot be trusted."""


class LocalEndpointEvidenceReader:
    """Read immutable file evidence only from explicitly authorized roots."""

    def __init__(self, allowed_roots: tuple[Path, ...]) -> None:
        if not allowed_roots:
            raise ValueError("endpoint D requires at least one allowed evidence root")
        self.allowed_roots = tuple(path.resolve(strict=True) for path in allowed_roots)
        self.verified_paths: set[Path] = set()

    def path_from_uri(self, uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise EndpointEvidenceError("endpoint D accepts only local file evidence")
        raw = unquote(parsed.path)
        if len(raw) >= 3 and raw[0] == "/" and raw[2] == ":":
            raw = raw[1:]
        path = Path(raw).resolve(strict=True)
        if not any(path == root or root in path.parents for root in self.allowed_roots):
            raise EndpointEvidenceError("endpoint evidence escaped the authorized roots")
        return path

    def read_verified(self, path: Path, expected_hash: str, *, limit: int) -> bytes:
        resolved = path.resolve(strict=True)
        if resolved != path or path.is_symlink() or not path.is_file():
            raise EndpointEvidenceError(f"unsafe endpoint evidence file: {path.name}")
        before = path.stat()
        if before.st_size < 1 or before.st_size > limit:
            raise EndpointEvidenceError(f"endpoint evidence size is invalid: {path.name}")
        data = path.read_bytes()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise EndpointEvidenceError(f"endpoint evidence changed during reread: {path.name}")
        actual = "sha256:" + hashlib.sha256(data).hexdigest()
        if actual != expected_hash:
            raise EndpointEvidenceError(f"endpoint evidence Hash differs: {path.name}")
        self.verified_paths.add(path)
        return data


def adjudicate_endpoint_campaign(
    request: EndpointFormalAdjudicationRequest,
    *,
    allowed_roots: tuple[Path, ...],
) -> EndpointFormalAdjudicationResult:
    """Reread all campaign evidence and publish a D verdict or ``invalid``."""

    reader = LocalEndpointEvidenceReader(allowed_roots)
    try:
        manifest_path = reader.path_from_uri(request.raw_evidence_manifest_uri)
        manifest_bytes = reader.read_verified(
            manifest_path,
            request.raw_evidence_manifest_sha256,
            limit=64 * 1024 * 1024,
        )
        manifest = _json_object(manifest_bytes, "raw evidence manifest")
        if not manifest or any(
            not isinstance(path, str) or not _is_sha256(value)
            for path, value in manifest.items()
        ):
            raise EndpointEvidenceError("raw evidence manifest is malformed")

        identities: set[tuple[int, str]] = set()
        cache_hashes: set[str] = set()
        group_results: list[EndpointAdjudicationGroupResult] = []
        all_baseline_means: list[float] = []
        all_candidate_means: list[float] = []

        for group in request.groups:
            acquisition_means: list[float] = []
            for acquisition in group.acquisitions:
                root = reader.path_from_uri(acquisition.evidence_uri)
                if not root.is_dir():
                    raise EndpointEvidenceError("endpoint acquisition URI is not a directory")
                _verify_tree(reader, root, manifest)
                result = _read_json(reader, root / "result.json", manifest)
                activation = _read_json(reader, root / "activation.json", manifest)
                cache = _read_json(reader, root / "cache-namespace.json", manifest)
                start = ProcessLifecycleRecordV2.model_validate(
                    _read_json(reader, root / "process-start.json", manifest)
                )
                exit_record = ProcessLifecycleRecordV2.model_validate(
                    _read_json(reader, root / "process-exit.json", manifest)
                )
                stop = _read_json(reader, root / "stop.json", manifest)
                cache_cleanup = _read_json(reader, root / "cache-cleanup.json", manifest)

                _require_hash(root / "result.json", manifest, acquisition.result_sha256)
                _require_hash(
                    root / "activation.json", manifest, acquisition.activation_sha256
                )
                _require_hash(
                    root / "cache-namespace.json",
                    manifest,
                    acquisition.cache_namespace_sha256,
                )
                _validate_acquisition_control(
                    request,
                    acquisition.arm,
                    acquisition.acquisition_ordinal,
                    result,
                    activation,
                    cache,
                    start,
                    exit_record,
                    stop,
                    cache_cleanup,
                )

                identity = (start.process_id, start.proc_stat_line)
                if identity in identities:
                    raise EndpointEvidenceError(
                        "endpoint acquisitions reused one service process identity"
                    )
                identities.add(identity)
                cache_identity = str(cache["namespace_hash"])
                if cache_identity in cache_hashes:
                    raise EndpointEvidenceError(
                        "endpoint acquisitions reused one cache namespace"
                    )
                cache_hashes.add(cache_identity)
                acquisition_means.append(
                    _read_acquisition_mean(reader, root, manifest, request)
                )

            baseline_mean = statistics.fmean(
                (acquisition_means[0], acquisition_means[3])
            )
            candidate_mean = statistics.fmean(
                (acquisition_means[1], acquisition_means[2])
            )
            log_ratio = math.log(candidate_mean / baseline_mean)
            all_baseline_means.extend((acquisition_means[0], acquisition_means[3]))
            all_candidate_means.extend((acquisition_means[1], acquisition_means[2]))
            group_results.append(
                EndpointAdjudicationGroupResult(
                    group_ordinal=group.group_ordinal,
                    baseline_mean_ns=baseline_mean,
                    candidate_mean_ns=candidate_mean,
                    log_ratio=log_ratio,
                    acquisition_means_ns=tuple(acquisition_means),
                )
            )

        log_ratios = [item.log_ratio for item in group_results]
        mean_log_ratio = statistics.fmean(log_ratios)
        standard_error = statistics.stdev(log_ratios) / math.sqrt(len(log_ratios))
        lower_log = mean_log_ratio - _T_95_DF7 * standard_error
        upper_log = mean_log_ratio + _T_95_DF7 * standard_error
        reduction = (1.0 - math.exp(mean_log_ratio)) * 100.0
        confidence_interval = (
            (1.0 - math.exp(upper_log)) * 100.0,
            (1.0 - math.exp(lower_log)) * 100.0,
        )
        if upper_log < 0.0:
            verdict = "faster"
        elif lower_log > 0.0:
            verdict = "slower"
        else:
            verdict = "inconclusive"
        return EndpointFormalAdjudicationResult(
            campaign_id=request.campaign_id,
            verdict=verdict,
            reason="D independently verified eight ABBA groups and the frozen 95% interval",
            successful_groups=8,
            measured_requests=(
                8 * 4 * request.group_plan.measured_requests_per_acquisition
            ),
            baseline_mean_ns=statistics.fmean(all_baseline_means),
            candidate_mean_ns=statistics.fmean(all_candidate_means),
            paired_latency_reduction_percent=reduction,
            confidence_interval_percent=confidence_interval,
            groups=tuple(group_results),
            verified_file_count=len(reader.verified_paths),
            manifest_sha256=request.raw_evidence_manifest_sha256,
        )
    except Exception as exc:
        return EndpointFormalAdjudicationResult(
            campaign_id=request.campaign_id,
            verdict="invalid",
            reason=f"{type(exc).__name__}: {exc}",
            successful_groups=0,
            measured_requests=0,
            verified_file_count=len(reader.verified_paths),
            manifest_sha256=request.raw_evidence_manifest_sha256,
        )


def _verify_tree(
    reader: LocalEndpointEvidenceReader, root: Path, manifest: dict[str, Any]
) -> None:
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files:
        raise EndpointEvidenceError("endpoint acquisition evidence tree is empty")
    prefix = root.as_posix().rstrip("/") + "/"
    actual_paths = {path.as_posix() for path in files}
    manifest_paths = {path for path in manifest if path.startswith(prefix)}
    if actual_paths != manifest_paths:
        raise EndpointEvidenceError("endpoint acquisition inventory differs from the manifest")
    for path in files:
        expected = manifest.get(path.as_posix())
        if not _is_sha256(expected):
            raise EndpointEvidenceError(
                f"endpoint evidence file is absent from the manifest: {path.name}"
            )
        reader.read_verified(path, expected, limit=64 * 1024 * 1024)


def _read_json(
    reader: LocalEndpointEvidenceReader, path: Path, manifest: dict[str, Any]
) -> dict[str, Any]:
    expected = manifest.get(path.as_posix())
    if not _is_sha256(expected):
        raise EndpointEvidenceError(f"endpoint JSON is absent from the manifest: {path.name}")
    return _json_object(reader.read_verified(path, expected, limit=16 * 1024 * 1024), path.name)


def _json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EndpointEvidenceError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise EndpointEvidenceError(f"{label} is not a JSON object")
    return value


def _require_hash(path: Path, manifest: dict[str, Any], expected: str) -> None:
    if manifest.get(path.as_posix()) != expected:
        raise EndpointEvidenceError(f"endpoint control-plane Hash differs: {path.name}")


def _validate_acquisition_control(
    request: EndpointFormalAdjudicationRequest,
    arm: str,
    ordinal: int,
    result: dict[str, Any],
    activation: dict[str, Any],
    cache: dict[str, Any],
    start: ProcessLifecycleRecordV2,
    exit_record: ProcessLifecycleRecordV2,
    stop: dict[str, Any],
    cache_cleanup: dict[str, Any],
) -> None:
    if (
        result.get("status") != "succeeded"
        or result.get("completed_requests")
        != request.group_plan.measured_requests_per_acquisition
        or result.get("expected_requests")
        != request.group_plan.measured_requests_per_acquisition
        or result.get("cleanup_succeeded") is not True
        or result.get("cache_cleanup_succeeded") is not True
        or result.get("producer_verdict") is not None
        or result.get("automatic_release_allowed") is not False
        or stop.get("cleanup_succeeded") is not True
        or cache_cleanup.get("removed") is not True
        or cache.get("empty_before_start") is not True
    ):
        raise EndpointEvidenceError("endpoint acquisition result or cleanup failed closed")
    if (
        start.event != "started"
        or exit_record.event != "reaped"
        or start.restart_ordinal != ordinal
        or exit_record.restart_ordinal != ordinal
        or start.process_id != exit_record.process_id
        or exit_record.wait_status != 0
        or activation.get("process_id") != start.process_id
    ):
        raise EndpointEvidenceError("endpoint acquisition lifecycle is invalid")
    expected_module = (
        request.signed_m1.artifact_hash
        if arm == "candidate"
        else request.baseline_module_hash
    )
    if activation.get("module_sha256") != expected_module:
        raise EndpointEvidenceError(f"endpoint {arm} activation Hash differs")


def _read_acquisition_mean(
    reader: LocalEndpointEvidenceReader,
    root: Path,
    manifest: dict[str, Any],
    request: EndpointFormalAdjudicationRequest,
) -> float:
    request_root = root / "requests"
    directories = sorted(path for path in request_root.iterdir() if path.is_dir())
    expected_count = request.group_plan.measured_requests_per_acquisition
    _verify_warmups(reader, root, manifest, request)
    if len(directories) != expected_count:
        raise EndpointEvidenceError("endpoint measured request count differs from the plan")
    latencies: list[int] = []
    for ordinal, directory in enumerate(directories):
        if directory.name != f"{ordinal:04d}":
            raise EndpointEvidenceError("endpoint request ordinals are not contiguous")
        for name in ("request.json", "response.json"):
            _read_json(reader, directory / name, manifest)
        sample = _read_json(reader, directory / "sample.json", manifest)
        started = sample.get("started_monotonic_ns")
        finished = sample.get("finished_monotonic_ns")
        latency = sample.get("e2e_latency_ns")
        if (
            sample.get("request_ordinal") != ordinal
            or sample.get("measured") is not True
            or sample.get("succeeded") is not True
            or type(started) is not int
            or type(finished) is not int
            or type(latency) is not int
            or latency <= 0
            or finished - started != latency
            or sample.get("prompt_tokens") != request.workload.expected_prompt_tokens
            or sample.get("completion_tokens")
            != request.workload.expected_completion_tokens
            or sample.get("error") is not None
        ):
            raise EndpointEvidenceError("endpoint request sample differs from the protocol")
        latencies.append(latency)
    return statistics.fmean(latencies)


def _verify_warmups(
    reader: LocalEndpointEvidenceReader,
    root: Path,
    manifest: dict[str, Any],
    request: EndpointFormalAdjudicationRequest,
) -> None:
    warmup_root = root / "warmup"
    directories = sorted(path for path in warmup_root.iterdir() if path.is_dir())
    if len(directories) != request.group_plan.warmup_requests:
        raise EndpointEvidenceError("endpoint warmup request count differs from the plan")
    for ordinal, directory in enumerate(directories):
        if directory.name != f"{ordinal:04d}":
            raise EndpointEvidenceError("endpoint warmup ordinals are not contiguous")
        for name in ("request.json", "response.json"):
            _read_json(reader, directory / name, manifest)
        sample = _read_json(reader, directory / "sample.json", manifest)
        if (
            sample.get("request_ordinal") != ordinal
            or sample.get("measured") is not False
            or sample.get("succeeded") is not True
            or sample.get("prompt_tokens") != request.workload.expected_prompt_tokens
            or sample.get("completion_tokens")
            != request.workload.expected_completion_tokens
            or sample.get("error") is not None
        ):
            raise EndpointEvidenceError("endpoint warmup sample differs from the protocol")


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        return False
    return all(character in "0123456789abcdef" for character in value[7:])


__all__ = [
    "EndpointEvidenceError",
    "LocalEndpointEvidenceReader",
    "adjudicate_endpoint_campaign",
]
