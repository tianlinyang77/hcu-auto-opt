# Stage 0 independent verification

`s0-g0-v1` separates evidence collection from the decision that authorizes later
engineering work. The measurement adapter writes immutable raw evidence. The
D-owned verifier reads those bytes again, verifies their SHA-256 digest and all
run bindings, and derives the measurement, profiler, and hot-patch gates without
using producer summaries.

The reviewable protocol is kept in `config/stage0/s0-g0-v1.yaml`, and an
identical copy is shipped as package data under `hcuopt.evaluation.protocols`.
Formal verification compares the supplied canonical content to that installed
copy; using the same version name with different thresholds is rejected. The
hash of the canonical protocol document is part of every raw evidence binding
and final report. The v1 timing metric is locked to `kernel_elapsed` in `ns`;
four probes cannot agree on a caller-invented metric and still obtain Formal
authority.

## Formal evidence

The `measurement-evidence-v2` envelope binds evidence to the task, Stage 0 run,
Target snapshot and fingerprint, workload, probe type, protocol version and
hash, metric, unit, measurement ID, exclusive lease, resource, fencing token,
and real adapter provenance. Timing evidence also stores the complete plan,
raw clock calibration points, process identity for every restart, acquisition
order, ABBA arm and segment identity, device ticks, batching, and typed
environment observations. A process identity is the pair `(pid,
process_start_token)`, not a PID alone. The token is not caller-chosen text: D
derives `linux-proc-startticks:<starttime>` from the bound raw
`/proc/<pid>/stat` line. Every restart references a canonical start record and
a canonical reaping record. D requires the same procfs identity in both,
matches it to every sample, and accepts the exit only when the raw `waitpid`
result names that PID and contains a terminal wait status. Producer-owned
`process_alive` booleans are not part of the Formal schema.

Clock conversion is independently fitted by ordinary least squares over at
least three raw `(device_ticks, host_midpoint_ns)` points. The maximum absolute
fit residual is recomputed from that fit and conservatively adds each point's
host half-interval uncertainty. Calibration must finish before the bound run
telemetry and timing samples begin. Timer resolution is not inferred from
host call latency: evidence includes at least three raw positive device
tick-delta observations, and the verifier uses
`min(resolution_tick_deltas) * fitted_ns_per_tick`. Producer-supplied
`ns_per_tick`, residual, and resolution summaries must match these recomputed
values and never override them.

Formal timing samples are derived only from device ticks:

```text
sample_ns = (finished_device_ticks - started_device_ticks)
            * fitted_ns_per_tick / batch_iterations
```

Host monotonic timestamps remain audit evidence but are not a fallback timing
source. They must prove the acquisition order and non-overlap. Device ticks are
ordered within each process restart; a new process may use a new timer epoch.
Each restart is reduced to one mean before cross-restart statistics are
computed. Hampel/MAD marks observations for the quality gate; it never deletes
or silently changes a sample.

The Formal reader is deliberately POSIX-only and accepts only `file:` URIs
below its configured evidence root. Platforms without `openat` and
`O_NOFOLLOW` fail closed instead of using a race-prone portable fallback. It
opens without following links, bounds the read to 64 MiB, and checks file
identity before and after reading. It rejects path escapes, symbolic links,
non-regular files, digest mismatches, invalid UTF-8 or JSON, and JSON whose bytes
are not the repository's canonical representation.

## Gate ownership

The verifier derives three typed inputs:

- measurement trust from noise, known-signal and null-signal evidence;
- profiler capability (`full`, `degraded`, or `none`) by re-reading the bound
  tool-version output and original rocprof CSV or PyTorch trace. Parser
  versions are bound to the tool (`rocprof-csv-v1` or `torch-trace-v1`). The
  rocprof parser accepts vendor trace/statistics headers, and the PyTorch parser
  derives kernel records from `traceEvents`; a producer-normalized
  `kernel_name,duration_ns,...` CSV is rejected;
- hot-patch capability (`hot_patch`, `overlay_only`, or `none`) from activation,
  correctness, cache isolation, recovery and cleanup evidence. D parses typed
  Baseline/Candidate `SourceSnapshot` and `ArtifactManifest` documents, requires
  the Candidate to be a clean child of the Baseline, binds the Artifact back to
  that Candidate, and re-hashes the Artifact and every referenced state entry.
  Each phase carries a successful real `ExecutionRequest`/`ExecutionResult`,
  procfs/wait lifecycle records, the versioned execution stdout, separately
  hashed normalized output bytes and cache evidence. The stdout-derived process,
  activation marker, implementation Hash, replacement point and output Hash must
  agree with those raw references. The Baseline source must match the Target
  Lock repository, Commit and checkout. Runtime hot patch requires one shared
  execution identity; startup overlay requires three distinct request and
  process identities. For startup overlay, only Candidate may mount the Artifact,
  the mount must be read-only and bound to the Candidate container, and
  Baseline/Recovery must return to identical state, output and cache. A generic
  Artifact-mount observation cannot authorize the SGLang Python/Triton overlay.
  Producer-owned hashes or pass/fail booleans are not accepted.

It does not choose `ProjectMode`. The control-plane Barrier combines those
three inputs with the existing four-mode mapping. Producer-provided database
summary fields such as `gate_result`, CV, MDE, `detected`, `false_positive`, or
`capability` are deliberately outside the statistical input digest.

Synthetic evidence, Dry Run evidence, fake provenance, non-exclusive leases,
unhealthy cleanup, mismatched bindings, missing device timing or telemetry, and
corrupted files cannot obtain Formal authority. A statistically valid run may
still fail the measurement gate because of excessive CV/MDE, timer error,
environmental drift, an undetected known signal, a null false positive, cache
inconsistency, or too many marked outliers. Power is recorded but has no v1
threshold.

## Scope

This verifier does not launch processes, collect HCU samples, implement a
profiler or overlay, or reserve hardware. The control plane now projects the
immutable Target Snapshot and seven probe references into this verifier, ignores
producer summaries, rechecks the input digest inside the PostgreSQL finalize
transaction, and publishes hashed JSON and Markdown reports. The HCU 7 reservation
risk is recorded as accepted rather than falsely described as proven exclusivity.
Formal probe production remains disabled until B/C emit this exact v2 evidence.
In particular, S0-C must
publish its existing raw PyTorch trace and three isolated overlay executions in
these typed envelopes; translating only S0-C's summary fields is not sufficient.

Every human-readable report must retain this statement:

> MDE 仅绑定本次 Target、Workload、指标、协议和样本预算，不是机器永久属性；Stage 0 不构成优化收益或自动发布授权。
