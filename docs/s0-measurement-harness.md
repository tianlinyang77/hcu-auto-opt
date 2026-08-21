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

The current S0-B adapter still publishes the earlier evidence shape. It therefore rejects
every Formal probe before touching the device; a Dry Run remains available for control-flow
and collector development. Formal execution may be enabled only after S0-B emits the strict
`measurement-evidence-v2` binding, raw lifecycle records, typed telemetry, and protocol Hash
consumed by `Stage0Verifier`.

## nmz36 formal-run procedure (after the v2 producer lands)

1. Confirm the Target Lock is still `nmz36-sglang-0.5.12`, including image digest, source
   commit, HCU 7, NUMA affinity, and every accepted risk.
2. `device_isolation_not_reserved` was explicitly accepted on 2026-08-21 so Stage 0 may
   start without a separately recorded reservation window. This is not evidence of physical
   exclusivity: Mooncake was still mapped to all devices at acceptance time. Pre/post
   telemetry must therefore record unmanaged HCU activity, and D must fail G0-M when an
   unmanaged accelerator process is observed. The acceptance cannot authorize performance
   publication or automatic release.
3. Start the unified `nmz36-stage0-v2` worker, supplying the locked-image workload
   callable, HCU device-timer implementation, telemetry collector, and explicit
   MeasurementPlan. The worker routes the five measurement probe types to S0-B and the
   `profiler`/`hotpatch` types to S0-C; there is intentionally no fallback host-only timer.
   For a container pinned to physical HCU 7, set `ROCR_VISIBLE_DEVICES=7` only. ROCR then
   exposes that physical device as logical device 0; adding `HIP_VISIBLE_DEVICES=7` after
   that remapping hides the only visible device. `TorchCudaEventTimer` uses a synchronized
   `torch.cuda.Event` origin and records device-relative nanosecond ticks from it. The
   device-to-host clock ratio and the smallest positive paired-Event interval are separate
   fields; only the latter is reported as `timer_resolution_ns`.
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

## Expected evidence

- `fingerprint`: hardware and software fingerprint hashes plus raw source evidence.
- `timer`: measured paired-Event resolution, device-to-host calibration ratio, residual,
  point count, and raw samples.
- `noise`: raw samples and statistics-input URI/hash, without B-generated `gate_result`.
  Formal samples include a PID and process-start token for every restart group; identities
  are distinct and every workload process is confirmed stopped before the next group.
- `known_signal` / `null_signal`: raw samples plus detector outcome; no speedup claim.

Any missing raw hash, unhealthy cleanup, lost lease, missing device timer, or unavailable
exclusive reservation is a failure, not a degraded performance conclusion.
