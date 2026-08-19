from collections.abc import Mapping
from typing import Any

from hcuopt.domain.enums import (
    GateResult,
    HotPatchCapability,
    ProfilerCapability,
    ProjectMode,
    Stage0ProbeType,
)
from hcuopt.domain.errors import ContractError
from hcuopt.domain.models import Stage0Evidence, Stage0Report

REQUIRED_STAGE0_PROBES = frozenset(Stage0ProbeType)


def _required_text(summary: Mapping[str, Any], name: str) -> str:
    value = summary.get(name)
    if not isinstance(value, str) or not value:
        raise ContractError(f"Stage 0 probe summary requires non-empty {name}")
    return value


def _required_number(summary: Mapping[str, Any], name: str) -> float:
    value = summary.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ContractError(f"Stage 0 probe summary requires non-negative {name}")
    return float(value)


def evidence_from_probe_summaries(
    probes: Mapping[Stage0ProbeType, Mapping[str, Any]],
    *,
    evidence_uri: str,
) -> Stage0Evidence:
    """Build typed gate evidence from the complete, server-bound probe family."""

    missing = sorted(item.value for item in REQUIRED_STAGE0_PROBES - probes.keys())
    if missing:
        raise ContractError(f"Stage 0 probe barrier is missing: {', '.join(missing)}")

    fingerprint = probes[Stage0ProbeType.FINGERPRINT]
    timer = probes[Stage0ProbeType.TIMER]
    noise = probes[Stage0ProbeType.NOISE]
    known_signal = probes[Stage0ProbeType.KNOWN_SIGNAL]
    null_signal = probes[Stage0ProbeType.NULL_SIGNAL]
    profiler = probes[Stage0ProbeType.PROFILER]
    hotpatch = probes[Stage0ProbeType.HOTPATCH]

    if known_signal.get("detected") is not True:
        measurement = GateResult.FAIL
    elif null_signal.get("false_positive") is not False:
        measurement = GateResult.FAIL
    else:
        try:
            measurement = GateResult(noise.get("gate_result"))
        except (TypeError, ValueError) as exc:
            raise ContractError("noise probe requires gate_result=pass|fail") from exc

    try:
        profiler_capability = ProfilerCapability(profiler.get("capability"))
    except (TypeError, ValueError) as exc:
        raise ContractError("profiler probe has an unknown capability") from exc
    try:
        hotpatch_capability = HotPatchCapability(hotpatch.get("capability"))
    except (TypeError, ValueError) as exc:
        raise ContractError("hotpatch probe has an unknown capability") from exc

    return Stage0Evidence(
        measurement=measurement,
        profiler=profiler_capability,
        hot_patch=hotpatch_capability,
        hardware_fingerprint=_required_text(fingerprint, "hardware_fingerprint"),
        software_fingerprint=_required_text(fingerprint, "software_fingerprint"),
        timer_resolution_ns=_required_number(timer, "timer_resolution_ns"),
        noise_sigma_ns=_required_number(noise, "noise_sigma_ns"),
        noise_cv=_required_number(noise, "noise_cv"),
        mde_ratio=_required_number(noise, "mde_ratio"),
        evidence_uri=evidence_uri,
    )


def evaluate_stage0(evidence: Stage0Evidence) -> Stage0Report:
    """Convert probe evidence into an explicit project capability mode.

    Automatic release remains disabled for every MVP mode. Stage 0 establishes
    whether engineering may continue; it does not grant production authority.
    """
    if evidence.measurement is GateResult.FAIL:
        return Stage0Report(
            mode=ProjectMode.STOPPED_MEASUREMENT,
            reasons=("测量闸门失败：停止性能实验和性能结论",),
        )

    reasons: list[str] = []
    if evidence.profiler is ProfilerCapability.NONE:
        reasons.append("Profiler 无可用热点数据：只保留配置轨道")
    if evidence.hot_patch is HotPatchCapability.NONE:
        reasons.append("热补丁与 overlay 均不可用：只保留配置轨道")
    if reasons:
        return Stage0Report(mode=ProjectMode.CONFIG_ONLY, reasons=tuple(reasons))

    if evidence.profiler is ProfilerCapability.DEGRADED:
        reasons.append("Profiler 能力降级：候选必须人工接入")
    if evidence.hot_patch is HotPatchCapability.OVERLAY_ONLY:
        reasons.append("只能启动时 overlay：每次候选需要独立服务进程")
    if reasons:
        return Stage0Report(
            mode=ProjectMode.DEGRADED_MANUAL_INTAKE,
            reasons=tuple(reasons),
        )

    return Stage0Report(
        mode=ProjectMode.FULL_MVP,
        reasons=("测量、Profiler 与热补丁探针通过",),
    )
