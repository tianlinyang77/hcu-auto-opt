# M2b Proposal independent verifier

This document defines the D-owned, read-only verification boundary for Issue #116.
The current implementation consumes the `m2b-agent-v1` contract from
`feat/m2b-agent-contract`; it does not execute an Agent, modify source, use an HCU,
measure performance, promote a package, or grant Formal Intake/release authority.

## Independent inputs

The verifier securely reopens canonical Knowledge Snapshot, Generation Request,
Apex Plan and A's `GenerationRunStatusView`. From that status it independently
reopens B's content-addressed Runner Execution Receipt and C's Proposal Batch,
raw output and Patch evidence. It checks each file SHA-256 and then independently
recomputes the Knowledge, Request, Plan, Receipt, Batch and Proposal identity
hashes. Attempt, Receipt, Batch, Request, Plan and Generation Run bindings are
checked as one chain. Producer summaries are not trusted.

Runner Receipt and Candidate Proposal Batch deliberately carry different provenance.
D requires `agent_runner` provenance on the Receipt and
`candidate_proposal_generation` provenance on the Batch, then joins A/B/C by
Attempt/Run/Request/Plan/generator identity, Generator Artifact Hash, Receipt and
Batch content Hashes, raw-output URI/SHA-256/bytes, cleanup and bounded usage. A
successful Runner Attempt may contain a succeeded or partial Batch, but never a
failed Batch. `GeneratorAttempt.actual`, the immutable Budget Ledger and the Receipt
must agree; failed no-Batch executions remain conservatively charged.

Every A `CandidateProposalRef` must bind one C Proposal identity/Hash/Batch/Patch.
D independently reproduces A's stable normalized-patch retained/duplicate relation,
then applies its stricter exact/normalized/identity/intent elimination order. A
duplicate must remain eliminated in D and point to the same retained normalized Patch.

Status members are canonicalized into Plan generator order, then Attempt and
Proposal ordinal order. Reversing evidence-reference completion order cannot
change the retained Proposal or input digest. A generator is conservatively
terminal only after one successful Attempt or after its bounded Attempt budget is
exhausted; missing generator evidence fails closed.

## Patch identity and elimination order

`normalized_patch_v1` performs only deterministic representation cleanup:

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
The caller cannot submit accepted/promoted lifecycle state. D will only expose a
transition after C publishes the versioned review and Promotion Receipt evidence
contracts and D can independently reread their content and bindings.

## Final vertical acceptance gate

Issue #116 is not complete until the stacked integration proves this chain with
the public A/B/C/D interfaces:

```text
Hotspot -> two Agent generators -> Apex retry/dedupe -> human approval
-> C Candidate Package promotion -> source-family verifier
-> existing M2a Scripted Intake
```

The final test must consume the real promotion and family interfaces. A test-only
mock is not acceptable evidence that the end-to-end requirement is complete.
