from hcuopt.domain.enums import (
    GateResult,
    HotPatchCapability,
    ProfilerCapability,
    ProjectMode,
)
from hcuopt.domain.models import Stage0Evidence, Stage0Report


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

