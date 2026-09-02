# M2a production Evidence Root and D Verifier

Issue: #127. This first no-HCU slice freezes the production read boundary used by
the independent D verifier. It does not register a Real Profile, create a Round,
authorize a window, sign off a result, or enable release.

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

- A: control-plane Context, plans and reveal authority;
- B: Search/Holdout measurements and cleanup receipts;
- C: Source and Artifact Family evidence;
- D: correctness, Barrier, FWER and final EvidenceBundle verification;
- project owner: human Signoff and exact window decision.

IDs and identity Hashes must be unique across roles. Evidence with a substituted
role or identity fails closed. One content Hash cannot be reused as evidence for
different Formal stages.

## D acceptance record

The versioned D review record can state only `accepted_for_formal_window` or
`blocked`. Acceptance requires non-empty verified evidence and no blockers;
blocked requires explicit, stable blocker codes. The record binds readiness,
Target Lock, Formal Authority Context, terminal path, Evidence Root, D Verifier,
verification input digest and summary object. Its signed Hash is evidence for the
later readiness audit, not project-owner window authorization.

`automatic_release_allowed=false` and `hcu_accessed=false` are permanent in this
slice.
