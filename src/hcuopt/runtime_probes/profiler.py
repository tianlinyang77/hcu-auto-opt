from __future__ import annotations

import csv
import gzip
import io
import json
import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from time import monotonic
from typing import Any

from hcuopt.adapters.execution import CommandResult, CommandRunner
from hcuopt.adapters.interfaces import ExecutionAdapter
from hcuopt.contracts.platform_v1 import ExecutionRequest, MountSpec, TargetSpec
from hcuopt.domain.enums import LeaseScope, ProfilerCapability
from hcuopt.runtime_probes.evidence import sha256_file
from hcuopt.runtime_probes.profile import ProfilerProbeConfiguration
from hcuopt.source_hash import file_uri_to_path

CORE_FIELDS = (
    "kernel_name",
    "duration_value",
    "duration_unit",
    "call_count",
    "kernel_category",
)
DETAIL_FIELDS = ("shape", "dtype", "meta", "python_location", "hip_location")
ALIASES = {
    "kernel_name": (
        "kernel_name",
        "kernel",
        "name",
        "KernelName",
        "Kernel_Name",
        "Name",
    ),
    "call_count": ("call_count", "calls", "count", "Calls"),
    "kernel_category": ("kernel_category", "category", "cat"),
    "shape": ("shape", "tensor_shape"),
    "dtype": ("dtype", "data_type"),
    "meta": ("meta", "metadata"),
    "python_location": ("python_location", "python_source", "python_stack"),
    "hip_location": ("hip_location", "hip_source", "hip_api"),
}


class ProfilerCapabilityProbe:
    """Run configured profiler commands and classify only fields actually observed."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        executor: ExecutionAdapter | None = None,
    ) -> None:
        if (runner is None) == (executor is None):
            raise ValueError("pass exactly one profiler command runner or container executor")
        self.runner = runner
        self.executor = executor

    def run(
        self,
        configuration: Mapping[str, Any],
        *,
        target: TargetSpec | None = None,
        output_dir: Path | None = None,
        resource_id: str | None = None,
        fencing_token: int | None = None,
        max_wall_seconds: int | None = None,
        require_formal_raw: bool = False,
    ) -> dict[str, Any]:
        if max_wall_seconds is not None and (
            isinstance(max_wall_seconds, bool) or max_wall_seconds < 1
        ):
            raise ValueError("profiler max_wall_seconds must be a positive integer")
        frozen = ProfilerProbeConfiguration.model_validate(configuration)
        configuration = frozen.model_dump(mode="json", exclude_none=True)
        capture_contract = self._capture_contract(configuration)
        candidates = frozen.tool_candidates

        attempts: list[dict[str, Any]] = []
        normalized_records: list[dict[str, Any]] = []
        selected_tool: str | None = None
        selected_raw: dict[str, Any] | None = None
        formal_raw: dict[str, Any] | None = None
        formal_score = -1
        best_score = -1
        deadline = monotonic() + max_wall_seconds if max_wall_seconds is not None else None
        for raw_candidate in candidates:
            candidate = raw_candidate
            name = candidate.name
            version_argv = candidate.version_argv
            profile_argv = candidate.profile_argv
            output_format = candidate.output_format
            timeout = float(candidate.timeout_seconds)
            if (
                output_format == "torch_trace"
                and self.executor is not None
                and candidate.output_host_uri is None
            ):
                attempts.append(
                    {
                        "tool": name,
                        "version_argv": list(version_argv),
                        "profile_argv": list(profile_argv),
                        "output_format": output_format,
                        "execution_error": (
                            "executor torch_trace requires a worker-owned output mount "
                            "and output_host_uri for container-to-host export"
                        ),
                    }
                )
                continue
            if output_format == "torch_trace" and self.executor is not None:
                self._validate_executor_trace_export(candidate, frozen.mounts)

            try:
                version = self._run_command(
                    version_argv,
                    self._remaining_timeout(min(timeout, 30.0), deadline),
                    configuration,
                    target,
                    output_dir,
                    resource_id,
                    fencing_token,
                )
            except TimeoutError:
                raise
            except OSError as error:
                attempts.append(
                    {
                        "tool": name,
                        "version_argv": list(version_argv),
                        "execution_error": f"{error.__class__.__name__}: {error}",
                    }
                )
                continue
            attempt: dict[str, Any] = {
                "tool": name,
                "version_argv": list(version_argv),
                "version_returncode": version.returncode,
                "version_stdout": self._decode(version.stdout),
                "version_stderr": self._decode(version.stderr),
            }
            if version.returncode != 0:
                attempts.append(attempt)
                continue

            trace_path: Path | None = None
            if output_format == "torch_trace":
                assert candidate.output_path is not None
                trace_path = (
                    file_uri_to_path(candidate.output_host_uri)
                    if self.executor is not None and candidate.output_host_uri is not None
                    else Path(candidate.output_path)
                )
                try:
                    self._prepare_trace_output(trace_path)
                except (OSError, ValueError) as error:
                    attempt["execution_error"] = f"{error.__class__.__name__}: {error}"
                    attempts.append(attempt)
                    continue

            try:
                profile = self._run_command(
                    profile_argv,
                    self._remaining_timeout(timeout, deadline),
                    configuration,
                    target,
                    output_dir,
                    resource_id,
                    fencing_token,
                )
            except TimeoutError:
                raise
            except OSError as error:
                attempt["execution_error"] = f"{error.__class__.__name__}: {error}"
                attempts.append(attempt)
                continue
            attempt.update(
                {
                    "profile_argv": list(profile_argv),
                    "profile_returncode": profile.returncode,
                    "profile_stdout": self._decode(profile.stdout),
                    "profile_stderr": self._decode(profile.stderr),
                    "output_format": output_format,
                }
            )
            attempts.append(attempt)
            if profile.returncode != 0:
                continue
            try:
                if output_format == "torch_trace":
                    assert trace_path is not None
                    trace_path = self._fresh_trace_path(trace_path)
                    attempt["raw_trace"] = {
                        "uri": trace_path.as_uri(),
                        "sha256": sha256_file(trace_path),
                        "byte_count": trace_path.stat().st_size,
                    }
                    records = self._parse_torch_trace(trace_path)
                    raw_output = trace_path.read_bytes()
                else:
                    records = self._parse_records(profile.stdout, output_format)
                    raw_output = profile.stdout
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
                attempt["parse_error"] = f"{error.__class__.__name__}: {error}"
                continue
            candidate_records = [self._normalize_record(item) for item in records]
            candidate_records = [item for item in candidate_records if item]
            candidate_score = max(
                (self._record_score(item) for item in candidate_records), default=-1
            )
            parser_version = {
                ("rocprof", "csv"): "rocprof-csv-v1",
                ("profile-llm-torch", "torch_trace"): "torch-trace-v1",
            }.get((name, output_format))
            if parser_version is not None and (
                candidate_score > formal_score or formal_raw is None
            ):
                formal_score = candidate_score
                formal_raw = {
                    "tool_name": name,
                    "output_format": output_format,
                    "tool_version_output": version.stdout,
                    "raw_output": raw_output,
                    "parser_version": parser_version,
                }
            if candidate_score > best_score or selected_raw is None:
                best_score = candidate_score
                normalized_records = candidate_records
                selected_tool = name
                selected_raw = {
                    "tool_name": name,
                    "output_format": output_format,
                    "tool_version_output": version.stdout,
                    "raw_output": raw_output,
                }
            if any(self._valid_core(item) for item in candidate_records) and (
                not require_formal_raw or parser_version is not None
            ):
                normalized_records = candidate_records
                selected_tool = name
                selected_raw = {
                    "tool_name": name,
                    "output_format": output_format,
                    "tool_version_output": version.stdout,
                    "raw_output": raw_output,
                }
                break

        observed = sorted(
            {
                field
                for record in normalized_records
                for field, value in record.items()
                if value not in (None, "", [], {})
            }
        )
        valid_records = [record for record in normalized_records if self._valid_core(record)]
        full_records = [
            record
            for record in valid_records
            if all(record.get(field) not in (None, "", [], {}) for field in DETAIL_FIELDS)
        ]
        best_record = max(
            valid_records or normalized_records,
            key=lambda record: sum(
                record.get(field) not in (None, "", [], {})
                for field in (*CORE_FIELDS, *DETAIL_FIELDS)
            ),
            default={},
        )
        if full_records:
            capability = ProfilerCapability.FULL
        elif valid_records:
            capability = ProfilerCapability.DEGRADED
        else:
            capability = ProfilerCapability.NONE

        return {
            "capability": capability.value,
            "selected_tool": selected_tool,
            "observed_fields": observed,
            "missing_fields": [
                field
                for field in (*CORE_FIELDS, *DETAIL_FIELDS)
                if best_record.get(field) in (None, "", [], {})
            ],
            "records": normalized_records,
            "valid_record_count": len(valid_records),
            "invalid_record_count": len(normalized_records) - len(valid_records),
            "attempts": attempts,
            "capture_contract": capture_contract,
            "triage_artifacts": self._triage_artifacts(configuration),
            "_formal_evidence": formal_raw,
        }

    @staticmethod
    def _remaining_timeout(requested: float, deadline: float | None) -> float:
        if deadline is None:
            return requested
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("runtime probe wall-clock budget exhausted")
        return min(requested, remaining)

    @staticmethod
    def _prepare_trace_output(path: Path) -> None:
        if path.is_symlink():
            raise ValueError("torch trace output path cannot be a symlink")
        if not path.exists():
            return
        if not path.is_file():
            raise ValueError("torch trace output path must be a regular file")
        path.unlink()

    @staticmethod
    def _validate_executor_trace_export(
        candidate: Any,
        mounts: tuple[MountSpec, ...],
    ) -> None:
        assert candidate.output_path is not None
        assert candidate.output_host_uri is not None
        host_output = file_uri_to_path(candidate.output_host_uri)
        host_parent = host_output.parent.resolve(strict=True)
        source = host_parent.as_posix()
        if os.name == "nt":
            source = f"/{source}"
        container_parent = str(Path(candidate.output_path).parent).replace("\\", "/")
        matching = [
            mount for mount in mounts if mount.source == source and mount.target == container_parent
        ]
        if len(matching) != 1 or matching[0].read_only:
            raise ValueError("executor torch_trace requires one worker-owned writable output mount")

    @staticmethod
    def _fresh_trace_path(path: Path) -> Path:
        if path.is_symlink() or not path.is_file():
            raise ValueError("profiler command did not produce a fresh torch trace")
        return path.resolve(strict=True)

    @staticmethod
    def _record_score(record: Mapping[str, Any]) -> int:
        return sum(
            record.get(field) not in (None, "", [], {}) for field in (*CORE_FIELDS, *DETAIL_FIELDS)
        )

    def _run_command(
        self,
        argv: tuple[str, ...],
        timeout: float,
        configuration: Mapping[str, Any],
        target: TargetSpec | None,
        output_dir: Path | None,
        resource_id: str | None,
        fencing_token: int | None,
    ) -> CommandResult:
        if self.runner is not None:
            return self.runner.run(argv, timeout)
        if self.executor is None or target is None or output_dir is None:
            raise ValueError("real profiler execution requires Target Lock and output directory")
        if (resource_id is None) != (fencing_token is None):
            raise ValueError("profiler resource_id and fencing_token must be present together")
        if resource_id is not None and not resource_id:
            raise ValueError("profiler resource_id cannot be empty")
        if fencing_token is not None and (
            isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token < 1
        ):
            raise ValueError("profiler fencing_token must be a positive integer")
        raw_mounts = configuration.get("mounts", [])
        if not isinstance(raw_mounts, list):
            raise ValueError("profiler mounts must be a list")
        mounts = [MountSpec.model_validate(item) for item in raw_mounts]
        request = ExecutionRequest(
            target_id=target.target_id,
            argv=list(argv),
            working_directory=str(configuration.get("working_directory", "/workspace")),
            environment={
                str(name): str(value)
                for name, value in self._mapping(
                    configuration.get("environment", {}), "profiler environment"
                ).items()
            },
            timeout_seconds=max(1, min(int(timeout), 86_400)),
            lease_scope=(LeaseScope.EXCLUSIVE if resource_id is not None else LeaseScope.NONE),
            resource_id=resource_id,
            fencing_token=fencing_token,
            container_image=target.inference_image.immutable_reference,
            mounts=mounts,
        )
        result = self.executor.execute(request, target, output_dir)
        return CommandResult(
            argv=argv,
            returncode=result.exit_code if result.exit_code is not None else 1,
            stdout=self._read_execution_stream(result.stdout_uri),
            stderr=self._read_execution_stream(result.stderr_uri),
        )

    @staticmethod
    def _read_execution_stream(uri: str | None) -> bytes:
        if uri is None:
            return b""
        if not uri.startswith("file:"):
            raise ValueError("profiler execution streams must use file URIs")
        path = file_uri_to_path(uri).resolve(strict=True)
        if path.is_symlink() or not path.is_file():
            raise ValueError("profiler execution stream is not a regular file")
        return path.read_bytes()

    @staticmethod
    def _capture_contract(configuration: Mapping[str, Any]) -> dict[str, Any]:
        workload = str(configuration.get("profile_workload", "both"))
        if workload not in {"both", "prefill", "decode"}:
            raise ValueError("S0-C profiler requires stage-separated both/prefill/decode capture")
        warmup_steps = int(configuration.get("warmup_steps", 10))
        active_steps = int(configuration.get("num_steps", 5))
        if warmup_steps < 1 or active_steps < 1:
            raise ValueError("profiler warmup_steps and num_steps must be positive")
        return {
            "workflow": "profile-llm-torch/triage",
            "profile_workload": workload,
            "warmup_steps": warmup_steps,
            "num_steps": active_steps,
            "prefill_input_len": int(configuration.get("prefill_input_len", 4090)),
            "prefill_output_len": int(configuration.get("prefill_output_len", 1)),
            "decode_input_len": int(configuration.get("decode_input_len", 1)),
            "decode_output_len": int(configuration.get("decode_output_len", 2048)),
        }

    @staticmethod
    def _triage_artifacts(configuration: Mapping[str, Any]) -> list[dict[str, str]]:
        value = configuration.get("triage_work_dir")
        if value is None:
            return []
        work_dir = Path(str(value)).resolve(strict=True)
        required = (
            "terminal_commands.log",
            "analysis_stdout.txt",
            "torch_profiler_analysis.md",
        )
        artifacts: list[dict[str, str]] = []
        for name in required:
            path = work_dir / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"profile-llm-torch triage artifact is missing: {path}")
            artifacts.append({"name": name, "uri": path.as_uri(), "sha256": sha256_file(path)})
        return artifacts

    @classmethod
    def _normalize_record(cls, raw: Mapping[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for canonical, aliases in ALIASES.items():
            for alias in aliases:
                if alias in raw and raw[alias] not in (None, ""):
                    normalized[canonical] = raw[alias]
                    break
        duration = cls._duration(raw)
        if duration is not None:
            normalized["duration_value"], normalized["duration_unit"] = duration
        return normalized

    @staticmethod
    def _duration(raw: Mapping[str, Any]) -> tuple[float, str] | None:
        choices = (
            ("duration_value", raw.get("duration_unit")),
            ("duration_ns", "ns"),
            ("DurationNs", "ns"),
            ("duration_us", "us"),
            ("DurationUs", "us"),
            ("duration_ms", "ms"),
            ("DurationMs", "ms"),
            ("duration", raw.get("duration_unit")),
        )
        for name, unit in choices:
            if name not in raw:
                continue
            value = raw[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
                or unit not in {"ns", "us", "ms", "s"}
            ):
                return None
            return float(value), str(unit)
        return None

    @staticmethod
    def _valid_core(record: Mapping[str, Any]) -> bool:
        kernel_name = record.get("kernel_name")
        call_count = record.get("call_count")
        category = record.get("kernel_category")
        return (
            isinstance(kernel_name, str)
            and bool(kernel_name.strip())
            and isinstance(call_count, int)
            and not isinstance(call_count, bool)
            and call_count > 0
            and category == "kernel"
            and isinstance(record.get("duration_value"), float)
            and record.get("duration_unit") in {"ns", "us", "ms", "s"}
        )

    @staticmethod
    def _parse_records(raw: bytes, output_format: str) -> list[Mapping[str, Any]]:
        text = raw.decode("utf-8", errors="replace")
        if output_format == "json":
            value = json.loads(text)
            if isinstance(value, dict):
                for key in ("records", "kernels", "results"):
                    if isinstance(value.get(key), list):
                        value = value[key]
                        break
                else:
                    value = [value]
            if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
                raise ValueError("profiler JSON output must contain an object list")
            return value
        if output_format == "jsonl":
            values = [json.loads(line) for line in text.splitlines() if line.strip()]
            if not all(isinstance(item, dict) for item in values):
                raise ValueError("profiler JSONL output must contain objects")
            return values
        if output_format == "csv":
            records = list(csv.DictReader(io.StringIO(text)))
            for record in records:
                for field in (
                    "duration_value",
                    "duration_ns",
                    "DurationNs",
                    "duration_us",
                    "DurationUs",
                    "duration_ms",
                    "DurationMs",
                    "duration",
                ):
                    if field in record:
                        try:
                            record[field] = float(record[field])
                        except (TypeError, ValueError):
                            pass
                for field in ALIASES["call_count"]:
                    if field in record:
                        try:
                            record[field] = int(record[field])
                        except (TypeError, ValueError):
                            pass
            return records
        raise ValueError(f"unsupported profiler output format: {output_format}")

    @classmethod
    def _parse_torch_trace(cls, path: Path) -> list[Mapping[str, Any]]:
        path = path.resolve(strict=True)
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as source:
                trace = json.load(source)
        else:
            with path.open("r", encoding="utf-8") as source:
                trace = json.load(source)
        events = trace.get("traceEvents", []) if isinstance(trace, dict) else trace
        if not isinstance(events, list):
            raise ValueError("torch trace must contain traceEvents")

        grouped: dict[str, dict[str, Any]] = {}
        for event in events:
            if not isinstance(event, Mapping) or event.get("ph") != "X":
                continue
            category = str(event.get("cat", "")).lower()
            args = event.get("args") if isinstance(event.get("args"), Mapping) else {}
            categories = {item.strip() for item in category.replace(";", ",").split(",")}
            if "kernel" not in categories:
                continue
            name = str(event.get("name", "")).strip()
            duration = event.get("dur")
            if not name or not isinstance(duration, (int, float)):
                continue
            row = grouped.setdefault(
                name,
                {
                    "kernel_name": name,
                    "duration_value": 0.0,
                    "duration_unit": "us",
                    "call_count": 0,
                    "kernel_category": "kernel",
                },
            )
            if not math.isfinite(float(duration)) or float(duration) <= 0:
                continue
            row["duration_value"] += float(duration)
            row["call_count"] += 1
            cls._copy_trace_detail(args, row)
        return list(grouped.values())

    @staticmethod
    def _copy_trace_detail(args: Mapping[str, Any], row: dict[str, Any]) -> None:
        trace_aliases = {
            "shape": ("Input Dims", "Input shapes", "shape"),
            "dtype": ("Input type", "dtype"),
            "meta": ("Concrete Inputs", "metadata", "meta"),
            "python_location": ("Python parent id", "Call stack", "python_location"),
            "hip_location": ("External id", "hip_api", "hip_location"),
        }
        for name, aliases in trace_aliases.items():
            if name in row:
                continue
            for alias in aliases:
                if args.get(alias) not in (None, "", [], {}):
                    row[name] = args[alias]
                    break

    @staticmethod
    def _mapping(value: Any, name: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{name} must be an object")
        return value

    @staticmethod
    def _required_text(value: Mapping[str, Any], name: str) -> str:
        result = value.get(name)
        if not isinstance(result, str) or not result:
            raise ValueError(f"profiler candidate requires {name}")
        return result

    @staticmethod
    def _argv(value: Any, name: str) -> tuple[str, ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ValueError(f"profiler candidate {name} must be an argv list")
        result = tuple(value)
        if not result or any(not isinstance(item, str) or not item for item in result):
            raise ValueError(f"profiler candidate {name} must contain non-empty strings")
        return result

    @staticmethod
    def _decode(value: bytes) -> str:
        return value.decode("utf-8", errors="replace")
