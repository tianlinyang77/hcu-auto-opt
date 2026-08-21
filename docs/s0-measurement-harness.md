# S0-B Measurement Harness

## Boundary

`hcuopt.measurement` is the sole producer of raw timing samples and timing evidence for
Stage 0 and future performance paths. It records identity, dynamic observations,
calibration points, restart groups, samples, and a SHA-256 over atomically written bytes.
It does not make a speedup, release, or G0-M statistical verdict.

The Stage 0 adapter owns `fingerprint`, `timer`, `noise`, `known_signal`, and
`null_signal`. The `noise` output intentionally contains only statistics input URI/hash
and sample count; D supplies the independent recomputation and `gate_result`. Until then,
the server-side Barrier remains fail-closed.

S0-B now has a separate Formal producer for the strict `measurement-evidence-v2`
binding, raw lifecycle records, typed telemetry, raw timer-resolution ticks, and
registered protocol Hash consumed by `Stage0Verifier`. The earlier evidence shape
remains available only to Dry Run. Formal execution fails before measurement unless
deployment supplies both a segmented external-process workload factory and an
executor-owned lifecycle recorder that captures `/proc/<pid>/stat` and raw `waitpid`
results. Landing this producer does not mean nmz36 has been measured.

## nmz36 formal-run procedure

1. Confirm the Target Lock is still `nmz36-sglang-0.5.12`, including image digest, source
   commit, HCU 7, NUMA affinity, and every accepted risk.
2. `device_isolation_not_reserved` was explicitly accepted on 2026-08-21 so Stage 0 may
   start without a separately recorded reservation window. This is not evidence of physical
   exclusivity: `mooncake_test_rpc` was explicitly stopped before Formal collection, but no
   independent reservation record exists. Pre/post
   telemetry must therefore record unmanaged HCU activity, and D must fail G0-M when an
   unmanaged accelerator process is observed. The acceptance cannot authorize performance
   publication or automatic release.
3. Start the unified `nmz36-stage0-v2` worker, supplying the locked-image segmented
   workload factory, HCU device-timer implementation, typed telemetry collector, raw
   process lifecycle recorder, and cleanup controller. The registered `s0-g0-v1`
   protocol creates the MeasurementPlan; callers cannot substitute thresholds or sample
   counts. The worker routes the five measurement probe types to S0-B and the
   `profiler`/`hotpatch` types to S0-C; there is intentionally no fallback host-only timer.
   For a container pinned to physical HCU 7, set `ROCR_VISIBLE_DEVICES=7` only. ROCR then
   exposes that physical device as logical device 0; adding `HIP_VISIBLE_DEVICES=7` after
   that remapping hides the only visible device. `TorchCudaEventTimer` uses a synchronized
   `torch.cuda.Event` origin and records device-relative nanosecond ticks from it. V2
   preserves every calibration interval and positive paired-Event tick delta; D refits
   the device-to-host clock ratio and derives `timer_resolution_ns` independently.
4. For each Formal Job, require `lease_scope=exclusive`, `lease_id`, `resource_id=hcu-7`,
   fencing token, and a live heartbeat. Capture pre/post HCU telemetry and background
   process state in the evidence.
5. Preserve the raw `file://` evidence and SHA-256. Evidence is published under the
   Stage 0 Run ID; equal bytes are idempotent and different bytes cannot replace an
   existing URI. On every exit path, fence only
   adapter-owned containers and require a healthy HCU/cleanup report before returning a
   `Stage0ProbeResult`.
6. Give the raw evidence to D for independent re-hashing and noise statistics. Only A's
   server-side finalization may open the Stage 0 Barrier or select a project mode. The API
   must set `HCUOPT_STAGE0_EVIDENCE_ROOT` to the deployment-owned tree shared with the
   workers; without it, Formal finalization fails closed.

`s0-g0-v1` compared Event resolution and clock-fit residual against the normalized
single-iteration mean. The first clean nmz36 run proved that this made a short batched
interval fail even when CV, MDE, known-signal, and null-signal checks were healthy.
That result remains immutable. Registered `s0-g0-v2` explicitly uses
`timer_gates.comparison_basis=raw_batch_interval` and fixes
`sampling.batch_iterations=5000`; it does not reinterpret v1 evidence under a new rule.

## Expected evidence

- `fingerprint`: hardware and software fingerprint hashes plus raw source evidence.
- `timer`: measured paired-Event resolution, device-to-host calibration ratio, residual,
  point count, and raw samples.
- `noise`: raw samples and statistics-input URI/hash, without B-generated `gate_result`.
  Formal samples include a PID and process-start token for every restart group; identities
  are distinct and every workload process is confirmed stopped before the next group.
- `known_signal` / `null_signal`: raw A1/B1/B2/A2 samples only. B does not publish
  `detected` or `false_positive`; those decisions belong exclusively to D.

Any missing raw hash, unhealthy cleanup, lost lease, missing device timer, or unavailable
logical exclusive lease is a failure, not a degraded performance conclusion. The accepted
physical-isolation risk is handled separately by mandatory background-process telemetry;
an unmanaged HCU process makes G0-M fail.
