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

    def validate_target(self, target: TargetSpec) -> None:
        self.require_framework_smoke()
        if self.implementation_kind == "real":
            blockers = [item.id for item in target.blockers if item.status == "open"]
            if blockers:
                raise TargetNotReady(
                    f"target {target.target_id} has open blockers for real execution: "
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
                    capabilities=FRAMEWORK_SMOKE_CAPABILITIES,
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
