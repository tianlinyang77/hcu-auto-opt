# M1-B Trusted Baseline/Candidate Measurement Design

## Scope

M1-B owns the raw performance-evidence producer for the single manually supplied
Candidate. It does not decide whether the Candidate is faster, slower, or acceptable.
That verdict remains an independent D-side operation.

The producer must reuse `hcuopt.measurement` device-event calibration, telemetry,
process-lifecycle, evidence hashing, lease fencing, and cleanup primitives. It must not
introduce a second timer or accept executable commands from the public Job payload.

## Boundary

The A control plane passes the immutable Baseline Epoch, Target snapshot, workload and
configuration hashes, Candidate Artifact, Formal Stage 0 report reference, budget, and
exclusive Job lease to the registered measurement harness. The C adapter remains
responsible for constructing a fresh baseline or startup-overlay process. B consumes a
small process-factory protocol, so scripted fixtures can run before C is merged and the
real factory can be registered later without changing evidence contracts.

The B result is one `MeasurementSeries` whose raw URI points to a canonical,
content-hashed `m1-measurement-evidence-v1` document. The document contains:

- immutable Task, Candidate, Baseline Epoch, Target, image, source, workload,
  configuration, Artifact, Stage 0 report, and exclusive-lease bindings;
- a hashed plan with ABBA acquisition order, warmups, samples, restart/process budget,
  metric, unit, and the current Stage 0 noise/MDE/confidence authority;
- raw device-event and host intervals, calibration inputs, process identities,
  acquisition order, telemetry, startup activation observations, and lifecycle records;
- no producer-authored performance verdict.

## Safety rules

Every acquisition starts a fresh process. Candidate acquisitions must attest the exact
Artifact content hash and startup Overlay mode; baseline acquisitions must attest that
no Candidate Artifact was loaded. Each process is closed in `finally`, must be reaped,
and the exclusive HCU resource is fenced and health-checked even when collection fails.

The harness reads and hashes the Formal Stage 0 machine report itself below a configured
trusted evidence root. Its verification section must match the Task's Stage0Run,
TargetSnapshot, Target, Workload, protocol hash, input digest, metric, unit, and a passing
measurement gate. The report's observed MDE, noise, and registered confidence settings
are copied into the plan. They are scoped to this run and are never treated as permanent
machine constants.

## Deferred integration

The real nmz36 startup-overlay process factory is deliberately deferred until M1-C
publishes its immutable Artifact/Overlay/import-attestation interface. This change ships
the frozen B evidence contract, strict report binding, reusable runner, worker dispatch,
and deterministic tests now; C integration should only supply the process factory.
