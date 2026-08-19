from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KnownSignalFixture:
    """Deterministic injected delay used to prove a detector can see a signal."""

    delta_ns: int

    def detected(self, *, threshold_ns: int) -> bool:
        return self.delta_ns > threshold_ns


@dataclass(frozen=True, slots=True)
class NullSignalFixture:
    """Deterministic no-op used to prove the detector does not invent a signal."""

    def detected(self, *, threshold_ns: int) -> bool:
        del threshold_ns
        return False
