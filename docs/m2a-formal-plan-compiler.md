# M2a Formal Plan Compiler

## Purpose

`FormalOperatorPlanCompiler` is the A2a control-plane boundary between an authorized
Formal Profile window and a future Formal StartIntent. It compiles an immutable Preview;
it does not create a Task, SearchRound, lease, measurement request, Holdout plan, signoff,
or release action.

The existing `OperatorPlanCompiler` remains Scripted-only. Formal compilation uses a
separate request, Plan, Preview, Repository Protocol, and compiler implementation so a
change to the real path cannot silently widen synthetic authority.

## Authoritative inputs

The compiler accepts:

1. an A1 `OperatorProfileCatalog` created through
   `build_formal_operator_profile_catalog()`;
2. the exact signed `FormalProfileWindowAuthorization` behind that Catalog;
3. a deployment-owned `FormalCandidateFamilyManifestStore` that resolves only the signed
   `source_family_hash` to the frozen C-owned `BusinessCandidateFamilyManifest`;
4. the deployment-owned `BusinessCandidateFamilyVerifier`;
5. a `FormalRoundPlanPreviewRequest` carrying exact Profile refs but no Family or Package
   content;
6. a `FormalOperatorAuthorityRepository` that rereads the Target, finalized Formal Stage
   0, immutable Baseline Epoch and source, Workload, and business Hotspot.

The request does not contain a client-selected Candidate list or Family Manifest. The
Manifest is fetched by the authorization Hash, its members are reread from the Candidate
Store, then sorted canonically by `candidate_id`; the compiler assigns contiguous Round
ordinals and freezes the resulting mapping in the Plan Hash.

## Fail-closed checks

Compilation blocks or rejects the Preview when any of the following changes:

- service identity or exact Profile Hash;
- signed Formal authorization Hash or active time window;
- authorized budget;
- Candidate Store ID or Hash;
- verified `source_family_hash` or Candidate count;
- Target Snapshot, Stage 0 run/protocol, Baseline Epoch/source, Workload, or Hotspot;
- Candidate package content or Candidate identity availability.

The resolved Plan commits the authorization Hash, host, resource, window, Profile refs,
Repository authority, canonical Candidate mapping, source Family Hash, Search/Holdout
protocol Hashes, selection rule, and budget. Preview expiry is the earlier of the local
Preview TTL and the owner-authorized window expiry.

`PostgresRepository.resolve_formal_operator_authority()` additionally rejects synthetic
Stage 0/source evidence, fake source provenance, a non-business Hotspot, a mutable or
non-M1 Baseline, and any mismatch against the exact IDs in the frozen Family Manifest.

## Deliberate A2a boundary

This slice does not define B's production Formal Adapter identity, lease/fencing/cleanup
receipts, or D's sealed Holdout Plan Authority and production Evidence root. Those inputs
must be consumed by the later A3 StartIntent only after the B and D contracts are frozen.

A2a always emits the `formal_start_authority_not_bound` blocking check, so
`start_allowed=false` even when the resolved Plan itself is complete. A3 may remove that
interlock only after consuming the separately frozen B and D authorities. No HCU access,
Candidate execution, performance claim, signoff, release, or concrete resource-window
authorization is provided by this implementation.
