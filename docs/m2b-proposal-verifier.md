# M2b Proposal independent verifier

This document defines the D-owned, read-only verification boundary for Issue #116.
The current implementation consumes the `m2b-agent-v1` contract from
`feat/m2b-agent-contract`; it does not execute an Agent, modify source, use an HCU,
measure performance, promote a package, or grant Formal Intake/release authority.

## Independent inputs

The verifier securely reopens canonical Knowledge Snapshot, Generation Request,
Apex Plan, Attempt and Proposal Batch evidence. It checks each file SHA-256 and
then independently recomputes the Knowledge, Request, Plan and Proposal identity
hashes. Attempt, Batch, Request, Plan and Generation Run bindings are checked as
one chain. Producer summaries are not trusted.

Runner Attempt evidence and Candidate Proposal Batch evidence deliberately carry
different provenance. D requires `agent_runner` provenance on the Attempt and
`candidate_proposal_generation` provenance on the Batch, then joins the layers by
Request Hash, Batch evidence Hash, raw-output URI/SHA-256, output bytes and bounded
usage. A successful Runner Attempt may contain a succeeded or partial Batch, but
never a failed Batch.

Attempt references are canonicalized into Plan generator order, then Attempt and
Proposal ordinal order. Reversing evidence-reference completion order cannot
change the retained Proposal or input digest. A generator is conservatively
terminal only after one successful Attempt or after its bounded Attempt budget is
exhausted; missing generator evidence fails closed until the durable Apex
authority snapshot is available for the final integration.

## Patch identity and elimination order

The verifier imports the sole public
`hcuopt.agent.patch_identity.normalize_patch_v1` implementation from the M2b
Contract layer. It performs only deterministic representation cleanup:

1. require strict UTF-8 and reject NUL;
2. normalize line endings to LF and remove trailing horizontal whitespace;
3. omit Git `index` lines and timestamps after tabs on `---`/`+++` lines;
4. write exactly one trailing LF.

The declared normalized hash is recomputed from those bytes. Diff paths must be
safe relative paths and equal the Proposal `touched_paths`. Candidates are then
eliminated in this fixed order: exact patch, normalized patch, Candidate identity,
and normalized optimization intent. Every eliminated Proposal remains visible
with its reason code.

## Failure semantics

- A Runner timeout is recorded as `runner_timeout`; if no Proposal remains, the
  aggregate status is `no_valid_proposals`.
- All duplicate or zero Proposal output is `no_valid_proposals`, not success.
- Cleanup failure, evidence tampering, unsafe paths, cross-binding and budget
  violations fail closed.
- Scripted evidence is always `synthetic`, `scripted_dev_only`,
  `performance_conclusion=not_measured`, `formal_intake_allowed=false` and
  `automatic_release_allowed=false`.

The UI read model copies the D verdict and authority flags. It may filter and
count rows but must not recalculate hashes, dedupe, infer performance, promote a
Proposal, or upgrade signoff.

Human review and package promotion are fixed to `pending` in this first tranche.
The caller cannot submit accepted/promoted lifecycle state. Although the shared
Contract now defines versioned Review and Promotion Receipt records, D will only
expose a transition after it re-reads the durable C-owned evidence and
independently verifies its content Hash and authority bindings.

## Final vertical acceptance gate

This first tranche deliberately stops before promotion because the stable A/B/C
outputs from Issues #113–#115 are not yet present on the parent branch. Issue
#116 is not complete until a later stacked change proves this real chain:

```text
Hotspot -> two Agent generators -> Apex retry/dedupe -> human approval
-> C Candidate Package promotion -> source-family verifier
-> existing M2a Scripted Intake
```

The final test must consume the real promotion and family interfaces. A test-only
mock is not acceptable evidence that the end-to-end requirement is complete.
