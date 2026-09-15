# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Known-default-auto transaction strategy; no hardware writer is implemented.

Uses the existing durable coordinator and Worker guard. Deployment must supply
both current control authority and independent default-baseline confirmation.
Design approval or a preflight JSON report is not either capability.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from hcuopt.deployment.bw20_clock_journal import ClockJournal
from hcuopt.deployment.bw20_clock_preflight import assess_clock_state
from hcuopt.deployment.bw20_clock_session import BW20ClockSession
from hcuopt.deployment.bw20_stage0_runtime import RESOURCE


class AutoClockBackend(Protocol):
    def observe(self) -> dict: ...
    def apply_sclk_level(self, *, level_index: int) -> None: ...
    def restore_default_auto(self) -> None: ...


@dataclass(frozen=True)
class DefaultAutoBaseline:
    """Default-auto identity, NOT a snapshot of enabled frequency masks.

    Instantaneous sclk is deliberately absent: idle frequency can change in auto.
    Equality checks the identity and settings that this limited strategy preserves.
    """

    host: str
    pci: str
    boot_id: str
    driver_header_sha256: str
    supported_sclk_mhz: tuple[int, ...]
    fixed_mclk_mhz: int = 1800
    sclk_overdrive: int = 0
    mclk_overdrive: int = 0
    gfx_boost: int = 0
    restoration_scope: str = "confirmed_default_auto_v1"


def _observe(raw, *, mode):
    report = assess_clock_state(raw)
    allowed_rejections = {"initial_mode_not_auto"} if mode == "manual" else set()
    if (raw["files"]["power_dpm_force_performance_level"].strip() != mode
            or set(report["rejection_reasons"]) - allowed_rejections):
        raise ValueError("observation outside confirmed-default-auto scope")
    header = raw.get("header_sha256")
    if not isinstance(header, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", header) is None:
        raise ValueError("driver interface fingerprint required")
    clocks = report["sclk"]
    baseline = DefaultAutoBaseline(
        host=raw["host"], pci=raw["pci"], boot_id=raw["boot_id"],
        driver_header_sha256=header,
        supported_sclk_mhz=tuple(clocks["supported_mhz"][i]
                                 for i in sorted(clocks["supported_mhz"])),
    )
    return baseline, clocks["supported_mhz"][clocks["current_index"]]


class BW20AutoClockSession(BW20ClockSession):
    """Only an injected backend can act; there is no SSH/sysfs/default writer.

    assert_default_baseline(snapshot) must independently confirm the baseline in
    the current authorized window, not simply echo observation.mode == 'auto'.
    Both callbacks raise on failure and must return None on success.
    """

    def __init__(self, *, backend: AutoClockBackend, assert_default_baseline, **kwargs):
        if not callable(assert_default_baseline):
            raise ValueError("independent default baseline confirmation required")
        for name in ("observe", "apply_sclk_level", "restore_default_auto"):
            if not callable(getattr(backend, name, None)):
                raise ValueError("explicit default-auto backend interface required")
        super().__init__(backend=backend, **kwargs)
        self.assert_default_baseline = assert_default_baseline

    def _capture_original(self):
        baseline, _ = _observe(self.backend.observe(), mode="auto")
        if self.assert_default_baseline(baseline) is not None:
            raise ValueError("default baseline was not independently confirmed")
        return baseline

    def _apply(self):
        # Observe again after durable intent/ownership check; a changed identity
        # or policy must not get a write using an old level index.
        baseline, _ = _observe(self.backend.observe(), mode="auto")
        if baseline != self.original:
            raise ValueError("default baseline changed before clock apply")
        self._owned()
        return self.backend.apply_sclk_level(
            level_index=self.original.supported_sclk_mhz.index(1500))

    def _verify_manual(self):
        baseline, current = _observe(self.backend.observe(), mode="manual")
        if baseline != self.original or current != 1500:
            raise ValueError("manual observation does not match requested clock scope")

    def _restore_original(self):
        # Do not restore onto a rebooted/replaced target, changed table, or changed
        # untouched setting. Such cases require separately authorized reconciliation.
        raw = self.backend.observe()
        mode = raw["files"]["power_dpm_force_performance_level"].strip()
        if mode not in {"auto", "manual"}:
            raise ValueError("unexpected mode before default-auto restore")
        baseline, _ = _observe(raw, mode=mode)
        if baseline != self.original:
            raise ValueError("target or preserved settings changed before restore")
        self._owned()
        if mode == "auto":
            # Idempotent recovery of a partial/no-op apply; no additional write.
            return None
        return self.backend.restore_default_auto()

    def _verify_original(self):
        baseline, _ = _observe(self.backend.observe(), mode="auto")
        if baseline != self.original:
            raise ValueError("default-auto baseline not observed after restore")

    def receipt(self):
        return {
            **super().receipt(),
            "restoration_scope": "confirmed_default_auto_v1",
            "enabled_mask_verified": False,
            "arbitrary_prior_policy_restored": False,
            "memory_clock_write_requested": False,
            # There is currently no approved hardware backend or hardware acceptance.
            "hardware_restore_verified": False,
        }


class BW20AutoClockSessionFactory:
    """Deployment-owned binding; still not a hardware backend or authorization source."""

    def __init__(
        self,
        *,
        backend: AutoClockBackend,
        journal: ClockJournal,
        authorization_id: str,
        assert_clock_authority,
        assert_default_baseline,
    ):
        if not isinstance(journal, ClockJournal):
            raise ValueError("persistent clock journal required")
        if not isinstance(authorization_id, str) or not authorization_id.strip():
            raise ValueError("explicit clock authorization reference required")
        if not callable(assert_clock_authority):
            raise ValueError("process-local clock authority check required")
        if not callable(assert_default_baseline):
            raise ValueError("independent default baseline confirmation required")
        for name in ("observe", "apply_sclk_level", "restore_default_auto"):
            if not callable(getattr(backend, name, None)):
                raise ValueError("explicit default-auto backend interface required")
        self.backend = backend
        self.journal = journal
        self.authorization_id = authorization_id
        self.assert_clock_authority = assert_clock_authority
        self.assert_default_baseline = assert_default_baseline

    def __call__(self, context, output_dir):
        del output_dir
        if not isinstance(context, Mapping):
            raise ValueError("trusted job context required")
        if (
            context.get("resource_id") != RESOURCE
            or context.get("lease_scope") != "exclusive"
            or type(context.get("fencing_token")) is not int
            or context["fencing_token"] < 1
        ):
            raise ValueError("exclusive BW20 resource identity required")
        assert_live_lease = context.get("assert_live_lease")
        if not callable(assert_live_lease):
            raise ValueError("process-local live lease check required")
        frozen_context = dict(context)

        def assert_control():
            if assert_live_lease() is not None:
                raise RuntimeError("live lease was not confirmed")
            if self.assert_clock_authority(frozen_context) is not None:
                raise RuntimeError("clock authority was not confirmed")

        return BW20AutoClockSession(
            backend=self.backend,
            journal=self.journal,
            resource_id=RESOURCE,
            authorization_id=self.authorization_id,
            assert_control=assert_control,
            assert_default_baseline=self.assert_default_baseline,
        )
