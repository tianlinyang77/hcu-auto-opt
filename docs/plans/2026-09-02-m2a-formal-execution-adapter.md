# M2a Formal Phase-aware Execution Adapter

## Goal

Deliver the B-line, no-HCU contract slice for issue #126. The change must keep
Scripted and Formal trust
domains separate, invoke the existing unique Measurement Harness through a narrow
protocol, and never compute or publish a performance verdict.

## Design

### Formal execution contract

Add a frozen Formal-only contract for one Candidate/Phase execution. It binds the
Formal Authority Context and Round/Candidate identities to:

- Search or Holdout phase and its distinct frozen phase/measurement plan hashes;
- A1 Formal Authorization/Resolved Plan, three Profile hashes and Target Lock refresh;
- target snapshot/profile, execution host identity and host content hash;
- one `hcu-<index>` plus its CPU/NUMA topology, exclusive Lease authority/receipt,
  renewal deadline, Fencing Token and approved window;
- one Round Budget reservation and one Job/attempt identity.

The request rejects missing Lease identity, non-exclusive scope, malformed HCU
resource/topology, naive/expired windows, an overdue or short Lease, a stale or
synthetic Target Lock refresh, and phase-specific Holdout/Search binding drift.

### Adapter lifecycle

`M2FormalPhaseExecutionAdapter` performs all preflight checks before reserving
budget. It re-reads the complete signed A1 authorization and A2a resolved plan
through deployment-owned interfaces and verifies their hashes, signature,
profiles, family, budget, window, Round, and Candidate. It then reserves through
the existing `M2RoundBudgetAuthority`, invokes
the injected unique Measurement Harness, independently re-reads/hashes the raw
evidence, verifies phase/target/lease/cleanup bindings, settles actual usage, and
publishes a content-addressed immutable execution Receipt.

If execution never starts, the reservation is released. Once the Harness starts,
success, timeout, stale Fence, malformed evidence, and cleanup failure are settled
and leave a failure Receipt. The adapter never creates a verdict and every Receipt
fixes `performance_conclusion=not_measured` and
`automatic_release_allowed=false`.

### Receipt immutability

Formal execution Receipts are content-addressed and publish-once. Search and
Holdout cannot reuse phase plan, measurement, raw evidence, process identity or
cache namespace identities. Successful Receipts embed the existing complete
`RoundMeasurementRef`; a durable SQLite-backed reference authority enforces
uniqueness across worker restarts and processes. Receipt and budget evidence
stores reject parent symlinks and publish atomically.

## TDD implementation plan

1. Add Contract tests for exclusive Lease, host/HCU/window/fencing bindings,
   phase-specific reveal rules, immutable Receipt shape and no-verdict policy.
2. Add Adapter tests for Search/Holdout separation, missing/expired Lease,
   stale Fence, insufficient budget, timeout, raw evidence tampering, cleanup
   failure, reserve/settle/release behavior and immutable Receipt publication.
3. Implement the Formal contract and content-addressed Receipt Store.
4. Implement the Formal adapter by reusing the existing Round Budget authority
   and the injected unique Harness protocol.
5. Update contract/ADR documentation.
6. Run focused tests, formatting/static checks, then the relevant M2/Agent
   regression suites.

## Explicit non-goals

- No nmz36 or real HCU access.
- No new Measurement Harness or statistical verifier.
- No Formal Round start, Holdout reveal authority, signoff or automatic release.
- No `faster`, speedup, latency or throughput conclusion.
