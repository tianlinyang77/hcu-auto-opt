# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Clock transaction coordinator, not a hardware writer or a clock authorization.

No default backend exists. A separately reviewed driver backend must capture and
restore the complete clock policy (including enabled levels, not instantaneous MHz).
The process-local ownership callback must be backed by real resource authority.
"""

from dataclasses import asdict, dataclass
from typing import Protocol

from hcuopt.deployment.bw20_clock_journal import ClockJournal
from hcuopt.deployment.bw20_stage0_runtime import RESOURCE


@dataclass(frozen=True)
class ClockPolicy:
    mode: str
    sclk_levels: tuple[int, ...]
    mclk_levels: tuple[int, ...]

    def __post_init__(self):
        if self.mode not in {"auto", "manual"}:
            raise ValueError("unsupported original clock policy")
        for levels in (self.sclk_levels, self.mclk_levels):
            if (not isinstance(levels, tuple) or not levels or len(set(levels)) != len(levels)
                    or any(type(n) is not int or n <= 0 for n in levels)):
                raise ValueError("complete enabled frequency levels are required")


class ClockBackend(Protocol):
    def capture_policy(self) -> ClockPolicy: ...
    def apply_manual(self, *, sclk_mhz: int, mclk_mhz: int) -> None: ...
    def restore_policy(self, policy: ClockPolicy) -> None: ...


class ClockReconciliationRequired(RuntimeError):
    """Keep the resource quarantined; an attempted restore is not a successful one."""


class BW20ClockSession:
    """Restore on normal exit and partial apply failure; never write after losing ownership."""

    def __init__(self, *, backend: ClockBackend, assert_control, resource_id: str,
                 authorization_id: str, journal: ClockJournal):
        if resource_id != RESOURCE or not callable(assert_control):
            raise ValueError("BW20 clock control ownership required")
        if not isinstance(authorization_id, str) or not authorization_id.strip():
            raise ValueError("explicit clock authorization reference required")
        self.backend, self.assert_control = backend, assert_control
        self.authorization_id = authorization_id
        if not isinstance(journal, ClockJournal):
            raise ValueError("durable clock journal required")
        self.journal, self.operation_id = journal, None
        self.journal_state = None
        self.original = None
        self.state = "new"
        self.events = []

    def _owned(self):
        if self.assert_control() is not None:
            raise RuntimeError("clock control ownership not confirmed")

    def _event(self, action, **fields):
        self.events.append(dict(action=action, **fields))

    def _persist(self, state):
        self.journal.transition(self.operation_id, expected=self.journal_state, state=state)
        self.journal_state = state

    def _capture_original(self):
        original = self.backend.capture_policy()
        if not isinstance(original, ClockPolicy):
            raise ValueError("complete restorable policy required")
        return original

    def _apply(self):
        return self.backend.apply_manual(sclk_mhz=1500, mclk_mhz=1800)

    def _verify_manual(self):
        if self.backend.capture_policy() != ClockPolicy("manual", (1500,), (1800,)):
            raise RuntimeError("requested manual clock policy was not observed")

    def _restore_original(self):
        return self.backend.restore_policy(self.original)

    def _verify_original(self):
        if self.backend.capture_policy() != self.original:
            raise RuntimeError("original clock policy was not observed after restore")

    def __enter__(self):
        if self.state != "new":
            raise RuntimeError("clock session cannot be reused")
        self.state = "checking"
        self._owned()
        if self.journal.unresolved(RESOURCE):
            raise ClockReconciliationRequired("unresolved durable clock intent blocks reuse")
        self.original = self._capture_original()
        self._event("policy_captured", **asdict(self.original))
        self._owned()
        # Commit original policy and mutation intent BEFORE the first device write.
        self.operation_id = self.journal.begin(
            resource_id=RESOURCE, authorization_id=self.authorization_id,
            original=asdict(self.original),
        )
        self.journal_state = "mutation_possible"
        # Mark uncertain BEFORE writing: the driver may apply partially and then raise.
        self.state = "mutation_possible"
        try:
            self._owned()
            if self._apply() is not None:
                raise RuntimeError("unexpected clock backend acknowledgement")
            self._owned()
            self._verify_manual()
            self._persist("active")
            self.state = "active"
            self._event("manual_policy_observed")
            return self
        except BaseException as exc:
            self._event("apply_failed", error_type=type(exc).__name__)
            self.restore()
            raise

    def restore(self):
        if self.state == "restored":
            return
        if self.state not in {"active", "mutation_possible"}:
            raise ClockReconciliationRequired("clock session is not safe for automatic restore")
        try:
            self._owned()
            self._persist("restoring")
            self._owned()
            if self._restore_original() is not None:
                raise RuntimeError("unexpected restore acknowledgement")
            self._owned()
            self._verify_original()
            self._owned()
            self._persist("restored")
        except BaseException as exc:
            self.state = "reconciliation_required"
            self._event("restore_unconfirmed", error_type=type(exc).__name__)
            try:
                self._persist("reconciliation_required")
            except BaseException as journal_exc:
                # A failed update leaves the prior nonterminal intent blocking reuse.
                self._event("journal_update_failed", error_type=type(journal_exc).__name__)
            raise ClockReconciliationRequired("clock policy restoration unconfirmed") from exc
        self.state = "restored"
        self._event("original_policy_observed")

    def __exit__(self, exc_type, exc, tb):
        self.restore()
        return False

    def receipt(self):
        return dict(resource_id=RESOURCE, authorization_id=self.authorization_id,
                    operation_id=self.operation_id, journal_state=self.journal_state,
                    state=self.state, restored=self.state == "restored",
                    quarantined=self.state != "restored", events=list(self.events),
                    stage0_accepted=False, automatic_release_allowed=False)
