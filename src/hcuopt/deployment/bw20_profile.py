# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Explicit Framework Smoke profile composition. Never waive Target blockers.

The build worker uses the three original CPU adapters in its isolated container;
the GPU worker supplies executor/evaluator/cleaner. Opt-in registration happens
only in the application's catalog, never by importing this module.
"""

from dataclasses import dataclass

from hcuopt.adapters.bw20_execution import BW20SmokeExecutionAdapter
from hcuopt.adapters.profiles import FRAMEWORK_SMOKE_CAPABILITIES, AdapterProfile
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.deployment.bw20_admission import BW20SmokeAdmission
from hcuopt.deployment.bw20_build_transport import BW20BuildJobHandler
from hcuopt.deployment.bw20_build_worker import PROFILE, validate_target
from hcuopt.deployment.bw20_pair_bridge import BW20PairCleaner, PreparedBW20Evaluator
from hcuopt.domain.errors import AdapterUnavailable, TargetNotReady


@dataclass(frozen=True)
class BW20FrameworkSmokeProfile(AdapterProfile):
    admission: BW20SmokeAdmission | None = None

    def validate_target(self, target, *, scope="framework_smoke",
                        evidence_resolved_blockers=frozenset()):
        if scope != "framework_smoke" or evidence_resolved_blockers:
            raise TargetNotReady("BW20 F1 profile cannot grant Stage0 or waive blockers")
        try:
            validate_target(target)
        except ValueError as exc:
            raise TargetNotReady(str(exc)) from exc
        super().validate_target(target, scope=scope)
        if self.admission is None:
            raise TargetNotReady("BW20 requires operator-pinned admission evidence")
        try:
            self.admission.validate(target)
        except (ValueError, OSError) as exc:
            raise TargetNotReady(str(exc)) from exc


def compose_framework_profile(*, build_handler: BW20BuildJobHandler,
                              gpu_registry: AdapterRegistry,
                              admission: BW20SmokeAdmission | None = None
                              ) -> BW20FrameworkSmokeProfile:
    if not isinstance(build_handler, BW20BuildJobHandler):
        raise AdapterUnavailable("BW20 requires the bounded CPU source/build transport")
    if gpu_registry.profile != PROFILE:
        raise AdapterUnavailable("BW20 worker registries must use the same explicit profile")
    expected = {"executor": BW20SmokeExecutionAdapter, "evaluator": PreparedBW20Evaluator,
                "resource_cleaner": BW20PairCleaner}
    for name, kind in expected.items():
        adapter = gpu_registry.require(name)
        if (not isinstance(adapter, kind)
                or adapter.provenance.implementation_kind != "real"):
            raise AdapterUnavailable(f"BW20 requires its real {name} boundary")
    return BW20FrameworkSmokeProfile(
        name=PROFILE, implementation_kind="real", capabilities=FRAMEWORK_SMOKE_CAPABILITIES,
        admission=admission)
