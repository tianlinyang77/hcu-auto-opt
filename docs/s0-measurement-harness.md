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

## nmz36 formal-run procedure

1. Confirm the Target Lock is still `nmz36-sglang-0.5.12`, including image digest, source
   commit, HCU 7, NUMA affinity, and the recorded exclusive reservation window.
2. Do not start a Formal run while `device_isolation_not_reserved` is open. A dry run may
   validate shape only and cannot be finalized as performance authority.
3. Start a worker with the distinct `nmz36-stage0-measurement-v2` profile, supplying the
   locked-image workload callable, HCU device-timer implementation, telemetry collector,
   and explicit MeasurementPlan. There is intentionally no fallback host-only timer.
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
   server-side finalization may open the Stage 0 Barrier or select a project mode.

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
