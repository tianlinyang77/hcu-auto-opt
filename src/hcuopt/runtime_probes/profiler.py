from __future__ import annotations

import csv
import gzip
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from hcuopt.adapters.execution import CommandResult, CommandRunner
from hcuopt.adapters.interfaces import ExecutionAdapter
from hcuopt.contracts.platform_v1 import ExecutionRequest, MountSpec, TargetSpec
from hcuopt.domain.enums import LeaseScope, ProfilerCapability
from hcuopt.runtime_probes.evidence import sha256_file

CORE_FIELDS = ("kernel_name", "duration", "call_count")
DETAIL_FIELDS = ("shape", "dtype", "meta", "python_location", "hip_location")
ALIASES = {
    "kernel_name": ("kernel_name", "kernel", "name", "KernelName", "Name"),
    "duration": ("duration", "duration_ns", "time", "DurationNs", "DurationUs"),
    "call_count": ("call_count", "calls", "count", "Calls"),
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
    ) -> dict[str, Any]:
        capture_contract = self._capture_contract(configuration)
        candidates = configuration.get("tool_candidates", [])
        if not isinstance(candidates, list):
            raise ValueError("profiler.tool_candidates must be a list")

        attempts: list[dict[str, Any]] = []
        normalized_records: list[dict[str, Any]] = []
        selected_tool: str | None = None
        for raw_candidate in candidates:
            candidate = self._mapping(raw_candidate, "profiler tool candidate")
            name = self._required_text(candidate, "name")
            version_argv = self._argv(candidate.get("version_argv"), "version_argv")
            profile_argv = self._argv(candidate.get("profile_argv"), "profile_argv")
            output_format = str(candidate.get("output_format", "json"))
            timeout = float(candidate.get("timeout_seconds", 300))
            if timeout <= 0:
                raise ValueError("profiler timeout_seconds must be positive")

            try:
                version = self._run_command(
                    version_argv,
                    min(timeout, 30.0),
                    configuration,
                    target,
                    output_dir,
                    resource_id,
                    fencing_token,
                )
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

            try:
                profile = self._run_command(
                    profile_argv,
                    timeout,
                    configuration,
                    target,
                    output_dir,
                    resource_id,
                    fencing_token,
                )
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
                    output_path = candidate.get("output_path")
                    if not isinstance(output_path, str) or not output_path:
                        raise ValueError("torch_trace profiler candidate requires output_path")
                    records = self._parse_torch_trace(Path(output_path))
                else:
                    records = self._parse_records(profile.stdout, output_format)
            except (UnicodeError, json.JSONDecodeError, ValueError) as error:
                attempt["parse_error"] = f"{error.__class__.__name__}: {error}"
                continue
            normalized_records = [self._normalize_record(item) for item in records]
            normalized_records = [item for item in normalized_records if item]
            if normalized_records:
                selected_tool = name
                break

        observed = sorted(
            {
                field
                for record in normalized_records
                for field, value in record.items()
                if value not in (None, "", [], {})
            }
        )
        core_present = all(field in observed for field in CORE_FIELDS)
        details_present = all(field in observed for field in DETAIL_FIELDS)
        if core_present and details_present:
            capability = ProfilerCapability.FULL
        elif core_present:
            capability = ProfilerCapability.DEGRADED
        else:
            capability = ProfilerCapability.NONE

        return {
            "capability": capability.value,
            "selected_tool": selected_tool,
            "observed_fields": observed,
            "missing_fields": [
                field for field in (*CORE_FIELDS, *DETAIL_FIELDS) if field not in observed
            ],
            "records": normalized_records,
            "attempts": attempts,
            "capture_contract": capture_contract,
            "triage_artifacts": self._triage_artifacts(configuration),
        }

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
        if (
            self.executor is None
            or target is None
            or output_dir is None
            or resource_id is None
            or fencing_token is None
        ):
            raise ValueError(
                "real profiler execution requires Target Lock, output directory, "
                "resource_id, and fencing_token"
            )
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
            lease_scope=LeaseScope.EXCLUSIVE,
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
        if not uri.startswith("file://"):
            raise ValueError("profiler execution streams must use file URIs")
        path = Path(uri.removeprefix("file://")).resolve(strict=True)
        if path.is_symlink() or not path.is_file():
            raise ValueError("profiler execution stream is not a regular file")
        return path.read_bytes()

    @staticmethod
    def _capture_contract(configuration: Mapping[str, Any]) -> dict[str, Any]:
        workload = str(configuration.get("profile_workload", "both"))
        if workload not in {"both", "prefill", "decode"}:
            raise ValueError(
                "S0-C profiler requires stage-separated both/prefill/decode capture"
            )
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
        return normalized

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
            return list(csv.DictReader(io.StringIO(text)))
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
            if "kernel" not in category and not any(
                key in args for key in ("kernel", "Kernel", "stream", "External id")
            ):
                continue
            name = str(event.get("name", "")).strip()
            duration = event.get("dur")
            if not name or not isinstance(duration, (int, float)):
                continue
            row = grouped.setdefault(
                name,
                {"kernel_name": name, "duration": 0.0, "call_count": 0},
            )
            row["duration"] += float(duration)
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
