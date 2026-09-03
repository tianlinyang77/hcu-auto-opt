# Durable Agent Generation Evidence Read Model

Issue #124 adds a D-owned, read-only projection for one terminal M2b Agent Generation
Run. It does not create a Candidate, execute an Agent, use an HCU, measure performance,
or grant Formal Intake.

## Trust boundary

The PostgreSQL row is an immutable index, not a cached authority verdict. Every API or
CLI read performs the following steps again:

1. Load the terminal A Generation Run, Attempt set, Proposal refs, and Budget ledger.
2. Read the persisted D publication and its exact verification context.
3. Re-read Knowledge, Request, Plan, A status, B Runner Receipts, C Proposal/Patch,
   Review, Promotion, and Family evidence from the configured evidence root.
4. Recompute every content Hash and the D verifier input digest.
5. Rebuild the read model and compare it byte-for-byte in meaning with the persisted
   verification, EvidenceBundle, read model, report, and manifest.
6. Return the shared `AgentGenerationReadModel` only when all checks pass.

Missing files, unsafe paths, symlinks, changed bytes, schema/version drift, a live/non-
terminal Run, cross-Run evidence, or a changed A authority snapshot fail closed.

## Interfaces

- API: `GET /v1/operator/agent-generations/{generation_run_id}/evidence`
- CLI: `hcuopt agent-generation-evidence <generation_run_id> --evidence-root <root>`
- Server-owned root: `HCUOPT_AGENT_EVIDENCE_ROOT`

There is deliberately no Web write endpoint. Publication is performed by the internal D
pipeline through `publish_agent_generation_evidence_publication()` after independent
verification and immutable report generation.

All returned models remain fixed to:

- `dev_only=true`
- `environment=scripted_dev_only`
- `performance_conclusion=not_measured`
- `formal_readiness=hold`
- `formal_intake_allowed=false`
- `automatic_release_allowed=false`

This read model is separate from the M2a production Formal Evidence Authority in #127.
