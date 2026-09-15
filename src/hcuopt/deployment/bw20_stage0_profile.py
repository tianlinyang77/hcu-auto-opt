# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Explicit seven-probe composition; imports never modify the public catalog."""

from dataclasses import dataclass

from hcuopt.adapters.profiles import STAGE0_CAPABILITIES, AdapterProfile
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.adapters.stage0_router import RoutedStage0ProbeAdapter
from hcuopt.deployment.bw20_stage0_adapter import BW20Stage0ProbeAdapter
from hcuopt.deployment.bw20_stage0_capabilities import BW20RuntimeProbeAdapter
from hcuopt.deployment.bw20_stage0_deployment import BW20MeasurementAdmission
from hcuopt.deployment.bw20_stage0_harness import PROFILE
from hcuopt.domain.enums import Stage0ProbeType
from hcuopt.domain.errors import AdapterUnavailable, TargetNotReady
from hcuopt.evaluation.stage0_protocol import load_registered_stage0_protocol
from hcuopt.targets import target_fingerprint


class BW20Stage0Router(RoutedStage0ProbeAdapter):
    def cleanup_probe(self, payload):
        probe = Stage0ProbeType(payload["probe_type"])
        # Keep the same concrete job/lease/fence context; no global cleaner fallback.
        return self._routes[probe].cleanup_probe(payload)


@dataclass(frozen=True)
class BW20Stage0Profile(AdapterProfile):
    admission: BW20MeasurementAdmission

    def validate_target(self, target, *, scope="stage0", evidence_resolved_blockers=frozenset()):
        if scope != "stage0" or evidence_resolved_blockers:
            raise TargetNotReady("BW20 Stage0 cannot waive blockers or grant other scopes")
        if target_fingerprint(target) != self.admission.target_hash:
            raise TargetNotReady("BW20 Stage0 target revision differs from deployment pin")
        if (
            load_registered_stage0_protocol(self.admission.protocol_version).sha256
            != self.admission.protocol_hash
        ):
            raise TargetNotReady("BW20 Stage0 protocol differs from deployment pin")
        super().validate_target(target, scope=scope)


def compose_stage0_profile(*, measurement, runtime, admission):
    if not isinstance(measurement, BW20Stage0ProbeAdapter) or not isinstance(
        runtime, BW20RuntimeProbeAdapter
    ):
        raise AdapterUnavailable("BW20 requires both concrete B and C job-scoped adapters")
    if (
        not isinstance(admission, BW20MeasurementAdmission)
        or runtime.admission != admission
        or getattr(measurement, "admission", None) != admission
    ):
        raise AdapterUnavailable("BW20 requires the same deployment admission")
    if any(
        a.provenance.profile != PROFILE or a.target_fingerprint != admission.target_hash
        for a in (measurement, runtime)
    ):
        raise AdapterUnavailable("BW20 adapters differ in profile or target revision")
    # Admission at launch is still required; this composes, it does not accept a run.
    routes = {
        probe: runtime if probe.value in runtime.supported_probe_types else measurement
        for probe in Stage0ProbeType
    }
    router = BW20Stage0Router(routes, profile=PROFILE)
    return (
        BW20Stage0Profile(PROFILE, "real", STAGE0_CAPABILITIES, admission),
        AdapterRegistry(profile=PROFILE, stage0_probe=router),
    )
