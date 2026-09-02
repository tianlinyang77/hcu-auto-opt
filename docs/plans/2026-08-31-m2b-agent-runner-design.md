# M2b Sandboxed Agent Runner Design

## Scope

Issue #114 needs a dev-only runner that executes an allowlisted local generator without a
shell, HCU, Holdout, measurement, SSH, or privileged-container authority. The runner returns
bounded proposal bytes and execution evidence. It does not parse or publish
`CandidateProposalBatch`; C owns that conversion after the proposal Contract is frozen.

## Chosen architecture

Add a B-owned runtime adapter in `hcuopt.adapters.agent_runner`. Its request/result types are
frozen dataclasses for an in-process adapter boundary, not durable Pydantic Contracts and not
extensions of `agent_v1`. A configured local-command runner receives one absolute executable,
an argv tuple, a fixed adapter-owned working directory, allowlisted environment values, and
normalized read-only input files. It launches with `shell=False` and a new process group.

Stdout and stderr are drained concurrently into bounded buffers. Crossing a stream or total
output limit, reaching the wall-clock deadline, or a normal root-process exit triggers verified
cleanup of the whole process tree. POSIX uses a process group; Windows creates the process
suspended, assigns it to a kill-on-close Job Object, and only then resumes it. The adapter always
removes its attempt directory. A cleanup failure changes the result to a closed failure and
suppresses proposal bytes.

Budget evidence is separate from M2/HCU budgets. Every result records one attempted generation,
elapsed wall time, stdout/stderr/total bytes, reported tokens, exit code, termination steps, and
cleanup status. Evidence also freezes Attempt/Run/Request authority, attempt number, Runner
provenance/profile identity, the verified Generator Artifact Hash, and the actual executable Hash.
The local command reports token usage through an adapter-owned JSON sidecar; missing or malformed
usage fails closed. Evidence stores hashes and redacted summaries, never environment values or raw
credentials.

The deterministic runner uses the same result/evidence shape without starting a process. It is
synthetic and exists for CI/scheduler tests only.

## Alternatives rejected

1. Implement `CandidateGeneratorAdapter` directly. PR #117 currently has requested Contract
   changes for request hashes, normalized patches, and review receipts, so this would couple B
   to unstable C-owned parsing and create rework.
2. Reuse the existing HCU `ExecutionAdapter`. That adapter carries Target, Lease, Docker, SSH,
   and device semantics that are explicitly forbidden for #114.
3. Accept an arbitrary shell command. This loses argv boundaries, expands the executable
   authority, and makes child cleanup and evidence unreliable.

## Failure and security boundaries

- Executables must be absolute, resolve to an allowlisted file, match an allowlisted argv prefix,
  and be recorded by content identity. A declared Generator Artifact must be the executable or an
  exact argv entry and its bytes must match the request's immutable SHA-256 identity.
- Input paths must be normalized relative paths and are staged read-only.
- Total staged input bytes are capped before an attempt directory or process is created.
- The process receives a constructed environment, not the parent environment. Secret, HCU,
  Holdout, SSH, Docker control, and adapter-reserved variables are rejected even when requested.
- Known HCU device or protected Holdout paths make the local-command preflight fail closed.
- Output overflow, timeout, non-zero exit, malformed usage, and cleanup failure never produce
  usable proposal bytes. Floating budgets reject NaN/Inf and integer budgets reject bool/float
  runtime values.
- This MVP is a bounded local-command adapter for a dedicated isolated worker; it is not a
  general-purpose hostile-code sandbox.

## Test strategy

Unit tests use a cross-platform Python stub and injected process/cleanup seams. They cover argv
literal preservation, environment filtering, read-only inputs, success evidence, non-zero exit,
timeout and child-tree termination, normal parent exit with a detached background child,
stdout/stderr/total limits, malformed usage, token overflow, identity replay/tamper attempts, host
isolation preflight, and failure-closed cleanup. Child-process tests use an explicit ready handshake
instead of a fixed startup sleep. The focused suite must pass on Windows and Linux; repository lint
and unit tests run before handoff.
