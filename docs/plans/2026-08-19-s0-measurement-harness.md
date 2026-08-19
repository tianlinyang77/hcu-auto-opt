# S0 Measurement Harness Implementation Plan

> **For Codex:** Execute this plan task-by-task, keeping hardware-dependent tests opt-in and fail-closed.

**Goal:** Implement B's single, reusable Measurement Harness for Stage 0 so timer, noise, Known Signal, and Null Signal probes emit immutable, independently recomputable evidence without making a performance verdict.

**Architecture:** Keep protocol mechanics in a pure `hcuopt.measurement` package and isolate HCU-specific collection behind injected clock, telemetry, and workload-callable interfaces. The Stage 0 worker turns one probe execution into the existing `Stage0ProbeResult`; the A-owned control plane remains the only writer of ProbeRecords and the only finalizer.

**Tech Stack:** Python 3.10, Pydantic contracts, `time.monotonic_ns`, standard-library JSON/SHA-256/atomic rename, pytest, existing adapter registry, lease and resource-cleaner boundaries.

---

## Constraints and non-goals

- The target is `nmz36-sglang-0.5.12`, HCU 7, with a current open `device_isolation_not_reserved` blocker. No formal run, no performance claim, and no HCU reservation may bypass Target Lock.
- The implementation is the sole timing/evidence producer for future performance paths. Profiler and hotpatch stay out of scope except for shared `Stage0ProbeResult` integration.
- A dry run may exercise Scripted/Fake fixtures only; it cannot become a formal conclusion. Formal runs require the real profile, exclusive lease, fencing token, cleanup health, raw URI, and SHA-256.

## Task 1: Define internal measurement protocol models

**Files:**
- Create: `src/hcuopt/measurement/__init__.py`
- Create: `src/hcuopt/measurement/models.py`
- Test: `tests/unit/test_measurement_models.py`

1. Add immutable dataclasses/Pydantic models for `MeasurementPlan`, `ClockCalibration`, `RawSample`, `DynamicObservation`, and `MeasurementEvidence`.
2. Require explicit warmups, repeats, process-restart count, batched-loop count, protocol version, and a stable environment fingerprint.
3. Reject synthetic data that claims real samples or a timing verdict; preserve raw values rather than statistics-only evidence.
4. Write failing validation tests, implement the minimum models, and run the focused pytest file.

## Task 2: Capture stable identity, dynamic observations, and immutable evidence

**Files:**
- Create: `src/hcuopt/measurement/fingerprint.py`
- Create: `src/hcuopt/measurement/evidence.py`
- Test: `tests/unit/test_measurement_evidence.py`

1. Produce canonical JSON (sorted keys, UTF-8, deterministic separators) and write it to a temporary sibling file before atomic replacement.
2. Hash exactly the bytes written and return a `file://` URI plus `sha256:<digest>`.
3. Split stable fingerprint inputs (target/image/source/runtime identity) from per-run dynamic observations (wall/monotonic timestamps, HCU telemetry, managed/background processes, cache and lease context).
4. Test byte-stable hashes, tamper detection, and that dynamic changes do not rewrite the stable fingerprint.

## Task 3: Implement clocks and sampling without HCU assumptions

**Files:**
- Create: `src/hcuopt/measurement/timers.py`
- Create: `src/hcuopt/measurement/sampling.py`
- Create: `src/hcuopt/measurement/fixtures.py`
- Test: `tests/unit/test_measurement_sampling.py`

1. Define injected host-monotonic and device-timer protocols; calibrate device ticks to host monotonic time and fail closed when the calibration is invalid or unavailable.
2. Run synchronisation, warmup, a short batched loop, repeated samples, and cross-process restart groups through a callable fixture interface.
3. Provide deterministic Scripted `KnownSignal` and `NullSignal` fixtures: Known must be detected; Null must never be reported as a signal.
4. Keep all raw samples, calibration points, restarts, and observations available to D; do not encode the final G0-M statistical verdict here.

## Task 4: Compose the unique Harness and produce MeasurementSeries

**Files:**
- Create: `src/hcuopt/measurement/harness.py`
- Test: `tests/unit/test_measurement_harness.py`

1. Compose fingerprint, telemetry, clocks, sampler, and evidence writer into one real `MeasurementHarness` implementation.
2. Generate `MeasurementSeries(status="measured")` only after evidence is atomically written and hash/URI/environment fingerprint are present.
3. Enforce formal execution context: exclusive lease, resource ID, fencing token, live-lease check before each launch, then fence and health check in `finally`.
4. Test timeout, workload exception, lease-loss, unhealthy cleanup, and evidence-write failure all fail closed and leave no measured result.

## Task 5: Connect the Harness to Stage 0 jobs and the real profile

**Files:**
- Modify: `src/hcuopt/adapters/interfaces.py`
- Modify: `src/hcuopt/adapters/registry.py`
- Modify: `src/hcuopt/adapters/profiles.py`
- Modify: `src/hcuopt/adapters/real_profile.py`
- Modify: `src/hcuopt/workers/handlers.py`
- Test: `tests/unit/test_stage0_measurement_handler.py`

1. Add an explicit Stage 0 probe adapter boundary rather than overloading the old generic performance handler.
2. Register a distinct real Stage 0 profile with `stage0_probe`; retain the existing F1 profile unchanged.
3. For fingerprint/timer/noise/known-signal/null-signal, map Harness outputs to the exact existing `Stage0ProbeResult.summary` fields and provenance list.
4. Require `cleanup_evidence` for formal results and submit nothing when the run is dry/synthetic or context is incomplete.

## Task 6: Verify contracts and prepare the target-bound runbook

**Files:**
- Create: `tests/integration/test_stage0_measurement_scripted.py`
- Create: `docs/s0-measurement-harness.md`

1. Exercise all B probes through Scripted adapters, with D-friendly raw evidence re-hashing and re-parsing.
2. Run `pytest tests/unit/test_measurement_*.py tests/unit/test_stage0_measurement_handler.py` and the existing Stage 0 control-plane tests; run `ruff check src tests`.
3. Document the gated nmz36 procedure: confirm Target Lock, acquire an exclusive HCU 7 window, capture pre/post telemetry, run the locked image, preserve evidence, and leave the result at `READY` until A/D integration.
4. Do not run this procedure while the isolation blocker is open; record the observed blocker instead.

## Verification sequence

1. Focused unit tests for each new module before integration.
2. Full non-Postgres test suite and Ruff before review.
3. Scripted end-to-end Stage 0 B probes, checking every generated hash by independently reading the stored bytes.
4. Only after a recorded exclusive HCU 7 window: opt-in target-lock test. Its result is evidence input, not a G0-M or release verdict.
