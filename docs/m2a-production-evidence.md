# M2a production Evidence Root and D Verifier

Issue: #127. The first no-HCU slice froze the production read boundary used by
the independent D verifier. The recursive slice now connects that boundary to
the existing Formal Round and Signoff finalizers. It does not register a Real
Profile, create a Round, authorize a window, execute HCU work, or enable release.

## Protected root

The deployment config supplies one immutable `ProductionEvidenceRootDescriptor`.
Its identity binds the root URI, layout version, access and retention policy
hashes, deployment owner, and maximum object size. Formal objects use exactly:

```text
<root>/objects/sha256/<first two digest characters>/<remaining 62 characters>
```

The verifier rejects arbitrary caller paths, paths outside the root, symlinks,
non-regular files, oversized files, content/path Hash disagreement, missing
objects, and changes during a read. The existing POSIX `openat`/`O_NOFOLLOW`
reader remains the physical read boundary; unsupported platforms fail closed.

## Verifier identity and role separation

`FormalEvidenceVerifierIdentity` binds the D verifier ID and version to its
executable Hash, configuration Hash, source commit, deployment attestation scheme
and key ID, and a content-addressed identity-evidence object. A Formal Authority
Context must bind the resulting identity Hash and the exact Evidence Root Hash.

The authority set keeps these principals distinct:

- A: control-plane Context and non-secret Search orchestration;
- B: Search/Holdout measurements and cleanup receipts;
- C: Source and Artifact Family evidence;
- D: Holdout Plan/Reveal authority, correctness, Barrier, FWER and final
  EvidenceBundle verification;
- project owner: human Signoff and exact window decision.

IDs and identity Hashes must be unique across roles. Evidence with a substituted
role or identity fails closed. One content Hash cannot be reused as evidence for
different Formal stages.

## Recursive D acceptance service

The caller supplies only a Round ID and readiness audit ID. A deployment-owned
Snapshot Reader resolves the frozen Evidence Root, Verifier, authorities and
content-addressed references. It does not accept caller-provided terminal models,
verification booleans or decisions.

The service performs these checks in order:

1. reread the Authority Context and verify every top-level object's content,
   size and Producer role;
2. parse the protected Round authority, Barrier, optional Reveal/FWER and final
   EvidenceBundle as canonical JSON;
3. reuse `M2FormalRoundFinalizer` to rebuild the complete Evidence Index and
   zero-promotion or Holdout/FWER terminal path;
4. reconstruct the immutable project-owner Signoff Intent from its protected
   Artifact, then reuse `M2FormalRoundSignoffFinalizer` for allowlisted signature
   verification;
5. bind Signoff back to the exact Round, Context, Bundle and Candidate/Artifact/
   Holdout Families;
6. publish and reread a verification summary before signing the D Review.

Only a complete chain can produce `accepted_for_formal_window` with no blockers.
Missing objects, non-canonical bytes, tampering, embedded cross-Round reuse,
wrong signers and rejected Signoff produce a signed `blocked` Review. A missing
or wrong-identity Snapshot, an unpublished summary or an invalid D signature
produces no Review.

Both accepted and blocked records bind readiness, Target Lock, Formal Authority
Context, Round, EvidenceBundle, Signoff, terminal path, Evidence Root, D Verifier,
verification input digest and summary object. The D signature is structured as
Verifier ID, key ID, identity Hash, algorithm and value; consumers must recompute
the Review Hash and use the deployment allowlist. The signed Review is evidence
for the later readiness audit, not project-owner window authorization.

These bindings and the structured signature are published as Review Schema v2.
The reserved v1 Contract never emitted a production acceptance record, so there
is no production v1 migration; readers must not reinterpret v1 bytes as v2.

Repository tests use deterministic injected keys. Production registrations and
keys remain deployment-owned and are never committed. `automatic_release_allowed=false`,
`hcu_accessed=false` and `owner_window_authorization=not_granted` remain permanent.

## Durable deployment registry and authenticated report

Migration 20 adds a PostgreSQL deployment registry for the frozen acceptance
Snapshot and the signed D Review. Each record stores canonical content plus its
SHA-256 identity. The `(round_id, readiness_audit_id)` binding and content Hash
are unique, identical retries are idempotent, conflicting bytes are rejected,
and database triggers prohibit updates and deletes. Reads rebuild the Pydantic
Contract and recompute the Snapshot Hash; Review reads also bind every readiness,
Context, Bundle, Signoff, Root and Verifier field back to that Snapshot.

`DeploymentFormalEvidenceAcceptanceRegistry.read()` is the concrete Snapshot
Reader consumed by the recursive acceptance service. Its Review publication and
read methods both execute the deployment allowlist signature verifier, so a
database row cannot become trusted merely because it has the right shape.

The API exposes only:

```text
GET /v1/operator/formal-rounds/{round_id}/evidence-acceptance
    ?readiness_audit_id=<deployment audit id>
```

The application must inject both a report service and an authentication callback.
Missing configuration fails with 503, denied access with 403, and changed stored
evidence with 422. No public registration, signing, acceptance, HCU execution or
release endpoint is added. The repository contains the migration and adapters,
but no production Snapshot, Review, secret or key.
