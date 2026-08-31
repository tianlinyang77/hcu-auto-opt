# M2b Sandboxed Agent Runner Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the dev-only, no-HCU Agent Runner and generation-budget evidence required by Issue #114.

**Architecture:** A B-owned runtime adapter launches one allowlisted executable with structured argv,
an adapter-created working directory, staged read-only inputs, and a constructed environment. It
captures bounded output, terminates the process tree on failure, removes temporary state, and returns
proposal bytes plus non-performance budget evidence; a deterministic implementation supports CI.

**Tech Stack:** Python 3.10+, standard-library subprocess/threading/pathlib/dataclasses, pytest, Ruff.

---

### Task 1: Freeze the runtime adapter behavior with failing tests

**Files:**
- Create: `tests/fixtures/agent_runner_stub.py`
- Create: `tests/unit/test_agent_runner.py`

**Step 1: Write the cross-platform stub**

Implement explicit modes for success, argv echo, environment echo, non-zero exit, sleep with a
child process, stdout/stderr overflow, and malformed/missing usage sidecar behavior.

**Step 2: Write failing safety and success tests**

Cover literal argv preservation, environment name allowlisting without evidence values, normalized
read-only inputs, proposal bytes, hashes, token/time/attempt/output consumption, and deterministic
runner identity.

**Step 3: Write failing negative-path tests**

Cover relative/unallowlisted executables, path traversal, secret/HCU/Holdout/SSH/Docker environment,
host device preflight, timeout, child cleanup, stream/total overflow, non-zero exit, malformed usage,
token overflow, and injected cleanup failure.

**Step 4: Run the focused suite and verify it fails**

Run: `python -m pytest tests/unit/test_agent_runner.py -q`

Expected: collection or import failure because `hcuopt.adapters.agent_runner` does not exist.

### Task 2: Implement the deterministic and local-command runners

**Files:**
- Create: `src/hcuopt/adapters/agent_runner.py`
- Modify: `src/hcuopt/adapters/__init__.py`

**Step 1: Add immutable runtime request, limits, evidence, and result dataclasses**

Validate normalized inputs, positive and coherent limits, hash format, exact executable identity,
and non-performance budget semantics without adding fields to `agent_v1`.

**Step 2: Add the deterministic CI runner**

Return fixed synthetic proposal bytes through the same evidence shape; reject configured output or
token consumption above the request limits.

**Step 3: Add the local-command runner**

Stage inputs, construct the environment, launch with `shell=False`, drain bounded streams, read the
usage sidecar, terminate the process group/tree when required, redact summaries, and always clean
the attempt directory.

**Step 4: Run focused tests until green**

Run: `python -m pytest tests/unit/test_agent_runner.py -q`

Expected: all tests pass on the current platform.

### Task 3: Verify integration and repository quality

**Files:**
- Modify only if required by verification: files introduced in Tasks 1-2.

**Step 1: Run adjacent Contract and adapter tests**

Run: `python -m pytest tests/unit/test_agent_contracts.py tests/unit/test_adapter_injection.py tests/unit/test_agent_runner.py -q`

Expected: all tests pass and `agent_v1` remains unchanged.

**Step 2: Run lint**

Run: `python -m ruff check src/hcuopt/adapters/agent_runner.py tests/unit/test_agent_runner.py tests/fixtures/agent_runner_stub.py`

Expected: no findings.

**Step 3: Run the full no-HCU unit suite**

Run: `python -m pytest tests/unit -q`

Expected: all tests pass; no HCU or target-lock marker is invoked.

**Step 4: Check Contract isolation and diff**

Run: `git diff --check && git diff -- src/hcuopt/contracts/agent_v1.py`

Expected: no whitespace errors and no changes to `agent_v1.py`.

### Task 4: Verify Linux behavior and prepare the stacked change

**Files:**
- No new files expected.

**Step 1: Transfer the branch commit to nmz36 without executing the real runner**

Use a Git bundle/patch and apply it in a dedicated remote worktree; do not disturb other users'
containers, HCU devices, or shared checkout changes.

**Step 2: Run focused Linux tests and lint**

Run the Task 3 focused commands in the remote worktree. The tests may launch only the Python stub;
they must not inspect or access HCU devices.

**Step 3: Review author identity before commit/push**

Confirm commit author and active GitHub account are `lvj-repox`; do not push while another account is
active.

**Step 4: Commit but wait for explicit push authorization**

Commit the verified implementation on `feat/m2b-agent-runner`. A future PR initially targets
`feat/m2b-agent-contract`, then retargets `main` after PR #117 merges.
