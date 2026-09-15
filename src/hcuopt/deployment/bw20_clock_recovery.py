# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Fail-closed recovery coordinator for an injected default-auto backend.

This module contains no sysfs, hy-smi, sudo or privileged writer. Deployment
must provide fresh recovery authority, quarantine proof and a reviewed backend.
"""

import json
import re
from dataclasses import dataclass

from hcuopt.deployment.bw20_auto_clock_session import (
    AutoClockBackend,
    DefaultAutoBaseline,
    _observe,
)
from hcuopt.deployment.bw20_clock_journal import ClockJournal, ClockJournalError
from hcuopt.deployment.bw20_stage0_runtime import RESOURCE


def _baseline_from_journal(value: str) -> DefaultAutoBaseline:
    try:
        raw = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ClockJournalError("clock journal original policy is invalid") from exc
    expected = {
        "host", "pci", "boot_id", "driver_header_sha256", "supported_sclk_mhz",
        "fixed_mclk_mhz", "sclk_overdrive", "mclk_overdrive", "gfx_boost",
        "restoration_scope",
    }
    if not isinstance(raw, dict) or set(raw) != expected:
        raise ClockJournalError("clock journal original policy is incomplete")
    levels = raw["supported_sclk_mhz"]
    if (
        not isinstance(levels, list)
        or not levels
        or len(levels) != len(set(levels))
        or any(type(level) is not int or level <= 0 for level in levels)
        or 1500 not in levels
        or raw["fixed_mclk_mhz"] != 1800
        or raw["sclk_overdrive"] != 0
        or raw["mclk_overdrive"] != 0
        or raw["gfx_boost"] != 0
        or raw["restoration_scope"] != "confirmed_default_auto_v1"
        or not all(isinstance(raw[key], str) and raw[key] for key in ("host", "pci", "boot_id"))
        or re.fullmatch(r"sha256:[0-9a-f]{64}", raw["driver_header_sha256"] or "") is None
    ):
        raise ClockJournalError("clock journal original policy is outside recovery scope")
    return DefaultAutoBaseline(**{**raw, "supported_sclk_mhz": tuple(levels)})


@dataclass(frozen=True)
class ClockRecoveryReceipt:
    operation_id: str
    attempt_id: str
    authorization_id: str
    fencing_token: int
    restored: bool
    write_requested: bool
    hardware_restore_verified: bool = False
    stage0_accepted: bool = False
    automatic_release_allowed: bool = False


class ClockRecoverySettlementError(ClockJournalError):
    """Keep both the recovery failure and its failed journal settlement visible."""

    def __init__(self, recovery_error: BaseException, settlement_error: BaseException):
        super().__init__(
            "clock recovery failed and reconciliation settlement also failed: "
            f"{type(settlement_error).__name__}: {settlement_error}"
        )
        self.recovery_error = recovery_error
        self.settlement_error = settlement_error


class BW20AutoClockRecovery:
    """One fresh, fenced takeover attempt; crashes remain claimed and quarantined."""

    def __init__(
        self,
        *,
        backend: AutoClockBackend,
        journal: ClockJournal,
        authorization_id: str,
        fencing_token: int,
        assert_recovery_control,
        assert_resource_quarantined,
    ):
        if not isinstance(journal, ClockJournal):
            raise ValueError("persistent clock journal required")
        if not isinstance(authorization_id, str) or not authorization_id.strip():
            raise ValueError("fresh recovery authorization reference required")
        if type(fencing_token) is not int or fencing_token < 1:
            raise ValueError("positive recovery fencing token required")
        if not callable(assert_recovery_control) or not callable(assert_resource_quarantined):
            raise ValueError("process-local recovery authority and quarantine checks required")
        for name in ("observe", "restore_default_auto"):
            if not callable(getattr(backend, name, None)):
                raise ValueError("explicit default-auto recovery backend required")
        self.backend = backend
        self.journal = journal
        self.authorization_id = authorization_id
        self.fencing_token = fencing_token
        self.assert_recovery_control = assert_recovery_control
        self.assert_resource_quarantined = assert_resource_quarantined

    def _owned(self):
        if self.assert_resource_quarantined(RESOURCE, self.fencing_token) is not None:
            raise RuntimeError("resource quarantine was not confirmed")
        if self.assert_recovery_control(RESOURCE, self.fencing_token) is not None:
            raise RuntimeError("fresh recovery control was not confirmed")

    def recover(self) -> ClockRecoveryReceipt:
        self._owned()
        pending = self.journal.unresolved(RESOURCE)
        if len(pending) != 1:
            raise ClockJournalError("exactly one unresolved clock operation is required")
        if pending[0]["authorization_id"] == self.authorization_id:
            raise ClockJournalError("recovery requires authority distinct from the original window")
        original = _baseline_from_journal(pending[0]["original_json"])
        raw = self.backend.observe()
        mode = raw.get("files", {}).get("power_dpm_force_performance_level", "").strip()
        if mode not in {"auto", "manual"}:
            raise ClockJournalError("current clock mode is outside default-auto recovery scope")
        observed, _ = _observe(raw, mode=mode)
        if observed != original:
            raise ClockJournalError("current target differs from journaled default-auto baseline")
        self._owned()
        claim = self.journal.claim_recovery(
            resource_id=RESOURCE,
            authorization_id=self.authorization_id,
            fencing_token=self.fencing_token,
        )
        write_requested = False
        try:
            self._owned()
            raw = self.backend.observe()
            mode = raw.get("files", {}).get("power_dpm_force_performance_level", "").strip()
            observed, _ = _observe(raw, mode=mode)
            if observed != original:
                raise RuntimeError("target changed after recovery claim")
            if mode == "manual":
                self._owned()
                write_requested = True
                if self.backend.restore_default_auto() is not None:
                    raise RuntimeError("unexpected recovery backend acknowledgement")
            self._owned()
            restored, _ = _observe(self.backend.observe(), mode="auto")
            if restored != original:
                raise RuntimeError("default-auto baseline not observed after recovery")
            self._owned()
            self.journal.finish_recovery(claim["attempt_id"], restored=True)
        except BaseException as recovery_error:
            try:
                self.journal.finish_recovery(claim["attempt_id"], restored=False)
            except BaseException as settlement_error:
                raise ClockRecoverySettlementError(
                    recovery_error, settlement_error
                ) from recovery_error
            raise
        return ClockRecoveryReceipt(
            operation_id=claim["operation_id"],
            attempt_id=claim["attempt_id"],
            authorization_id=self.authorization_id,
            fencing_token=self.fencing_token,
            restored=True,
            write_requested=write_requested,
        )
