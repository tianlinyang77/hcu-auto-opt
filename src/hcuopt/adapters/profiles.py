from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hcuopt.contracts.platform_v1 import TargetSpec
from hcuopt.contracts.v1 import AdapterProfileView
from hcuopt.domain.errors import AdapterUnavailable, TargetNotReady

FRAMEWORK_SMOKE_CAPABILITIES = frozenset(
    {
        "source_manager",
        "builder",
        "artifact_store",
        "executor",
        "evaluator",
        "resource_cleaner",
    }
)
STAGE0_CAPABILITIES = frozenset({"stage0_probe"})
REAL_FRAMEWORK_SMOKE_PROFILE = "nmz36-framework-smoke-v1"
REAL_STAGE0_MEASUREMENT_PROFILE = "nmz36-stage0-measurement-v2"
REAL_STAGE0_PROFILE = "nmz36-stage0-v1"


@dataclass(frozen=True, slots=True)
class AdapterProfile:
    name: str
    implementation_kind: Literal["real", "fake"]
    capabilities: frozenset[str]

    def require_framework_smoke(self) -> None:
        missing = sorted(FRAMEWORK_SMOKE_CAPABILITIES - self.capabilities)
        if missing:
            raise AdapterUnavailable(
                f"adapter profile {self.name} lacks Framework Smoke capabilities: "
                f"{', '.join(missing)}"
            )

    def require_stage0(self) -> None:
        missing = sorted(STAGE0_CAPABILITIES - self.capabilities)
        if missing:
            raise AdapterUnavailable(
                f"adapter profile {self.name} lacks Stage 0 capabilities: "
                f"{', '.join(missing)}"
            )

    def validate_target(
        self,
        target: TargetSpec,
        *,
        scope: str = "framework_smoke",
    ) -> None:
        if scope == "stage0":
            self.require_stage0()
        else:
            self.require_framework_smoke()
        if self.implementation_kind == "real":
            blockers = [
                item.id
                for item in target.blockers
                if item.status == "open" and scope in item.blocks
            ]
            if blockers:
                raise TargetNotReady(
                    f"target {target.target_id} has open blockers for {scope}: "
                    f"{', '.join(blockers)}"
                )

    def view(self) -> AdapterProfileView:
        return AdapterProfileView(
            name=self.name,
            implementation_kind=self.implementation_kind,
            required_capabilities=sorted(self.capabilities),
        )


class AdapterProfileCatalog:
    def __init__(self, profiles: tuple[AdapterProfile, ...] | None = None) -> None:
        selected = (
            profiles
            if profiles is not None
            else (
                AdapterProfile(
                    name="fake-v1-control-flow-only",
                    implementation_kind="fake",
                    capabilities=FRAMEWORK_SMOKE_CAPABILITIES | STAGE0_CAPABILITIES,
                ),
                AdapterProfile(
                    name=REAL_FRAMEWORK_SMOKE_PROFILE,
                    implementation_kind="real",
                    capabilities=FRAMEWORK_SMOKE_CAPABILITIES,
                ),
                AdapterProfile(
                    name=REAL_STAGE0_MEASUREMENT_PROFILE,
                    implementation_kind="real",
                    capabilities=STAGE0_CAPABILITIES,
                ),
                AdapterProfile(
                    name=REAL_STAGE0_PROFILE,
                    implementation_kind="real",
                    capabilities=STAGE0_CAPABILITIES,
                ),
            )
        )
        self._profiles = {profile.name: profile for profile in selected}
        if len(self._profiles) != len(selected):
            raise ValueError("adapter profile names must be unique")

    def require(self, name: str) -> AdapterProfile:
        try:
            return self._profiles[name]
        except KeyError as exc:
            raise AdapterUnavailable(f"adapter profile is not registered: {name}") from exc

    def list(self) -> list[AdapterProfileView]:
        return [self._profiles[name].view() for name in sorted(self._profiles)]
