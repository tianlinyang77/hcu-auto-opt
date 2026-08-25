# M1 Trusted Baseline/Candidate Measurement Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Produce hashed, independently recomputable Baseline/Candidate kernel timing
evidence for the M1 single-Candidate workflow without allowing B to author the verdict.

**Architecture:** Add strict M1 evidence models and a paired runner around the existing
device timer, calibration, telemetry, lifecycle, fencing, and evidence writer. Extend the
M1 Job payload with the immutable Formal Stage 0 report reference and dispatch
`manual_performance` through an explicit real-harness protocol. Keep the nmz36 process
factory injectable until M1-C freezes its Overlay launch contract.

**Tech Stack:** Python 3.10+, Pydantic v2, pytest, existing hcuopt measurement contracts.

---

### Task 1: Freeze M1 evidence and plan contracts

**Files:**
- Create: `src/hcuopt/measurement/m1_models.py`
- Modify: `src/hcuopt/measurement/__init__.py`
- Test: `tests/unit/test_m1_measurement.py`

Define strict immutable bindings, Stage 0 authority, plan, acquisition, activation,
sample, and evidence models. Enforce ABBA balance, Stage 0 sampling-budget equality,
unique process/cache identities, Candidate Artifact attestation, ordered samples,
lease-bound cleanup, and absence of a producer verdict. Record the shared decision in
ADR-0006 so D imports this Contract instead of defining a second schema.

### Task 2: Load and bind Formal Stage 0 authority

**Files:**
- Create: `src/hcuopt/measurement/m1_stage0.py`
- Test: `tests/unit/test_m1_measurement.py`

Use `Stage0EvidenceReader` to re-hash the machine report. Validate all control-plane
bindings and derive the current MDE/noise/confidence authority and a canonical plan hash.

### Task 3: Implement paired evidence collection

**Files:**
- Create: `src/hcuopt/measurement/m1_harness.py`
- Modify: `src/hcuopt/adapters/interfaces.py`
- Test: `tests/unit/test_m1_measurement.py`

Collect ABBA Baseline/Candidate acquisitions with a fresh external process per arm,
hashed child-process device-event records, warmups, telemetry, lifecycle records, lease
checks, continuous wall-clock budget checks, and finally cleanup. Write the success
evidence only after cleanup so the raw hash covers it. Return
`ManualPerformanceEvidenceResult` only.

### Task 4: Connect the durable M1 Job

**Files:**
- Modify: `src/hcuopt/storage/repository.py`
- Modify: `src/hcuopt/workers/handlers.py`
- Modify: `tests/integration/test_m1_control_plane_postgres.py`
- Test: `tests/unit/test_m1_measurement.py`

Carry the Formal Stage 0 machine-report URI/hash and input digest into the immutable Job
payload. Add explicit `manual_performance` dispatch that refuses a harness lacking the
M1 real-evidence interface.

### Task 5: Verify and document integration status

**Files:**
- Modify: `docs/m1-control-plane.md`

Run Ruff and focused unit tests, then the broader unit suite. Record that scripted no-op
and known-signal evidence are producer fixtures only; the D verifier owns their verdicts.
Prepare the nmz36 command and real C-factory integration checklist without claiming the
Target-locked test has run.
