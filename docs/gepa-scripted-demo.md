# GEPA Scripted proposal-policy demo

## Purpose

This opt-in experiment evolves an advisory proposal-generation policy using the
existing M2b Agent/Apex path and D's independently rebuilt Agent Generation Read
Model. It measures proposal yield only. The run remains `scripted_dev_only`,
`not_measured`, Formal `hold`, and `automatic_release_allowed=false`.

GEPA is the outer policy-search loop. Existing A/B/C/D services still own Run
creation and budgets, Agent execution and cleanup, Proposal/Package lifecycle,
and evidence verification. GEPA never receives HCU or Holdout access.

## Binding rule

The strategy text is published as an advisory-only entry in a new immutable
Knowledge Snapshot for every evaluation. The corresponding Generation Request
and Plan must bind that snapshot and their content Hashes; each policy evaluation
therefore creates a distinct Run. Rewriting Knowledge or strategy text under an
existing Request/Plan is forbidden.

The evaluator callback supplied to `run_gepa_scripted_demo()` must:

1. Publish the policy as a content-addressed Knowledge payload.
2. Create a new scripted M2b Generation Run using the existing A authority.
3. Execute the already configured Scripted Agent/Apex path and settle its
   existing Runner Receipts and Proposal Batches.
4. Invoke D's existing evidence read service, which rereads and re-hashes the
   stored evidence.
5. Return that D `AgentGenerationReadModel` and the independently computed
   Knowledge Snapshot Hash.

The bridge rejects target, baseline, replacement point, policy Knowledge Hash,
or Scripted authority drift. D's verified retained-Proposal yield is the only
score: `kept proposals / frozen max proposals`. Failure and invalid evidence
score zero and abort if D evidence cannot be verified. Feedback contains reason
codes, counts, budget, and content Hashes; it excludes raw patch previews.

## Small pilot

Use one fixed Scripted fixture and four GEPA metric calls first. That is at most
four evaluator calls and four Generation Runs. GEPA and the provider do not
share an evaluation budget, so any increase in metric calls must be explicitly
rebounded in both the wrapper and the existing Generation Plan budgets.

The wrapper is deliberately dependency-injected so importing hcu-auto-opt does
not install or require GEPA. In an isolated Python 3.10+ demo environment, pin
GEPA to 0.1.4 and configure its reflection model. The current GEPA API exposes
`optimize_anything`, `GEPAConfig`, `EngineConfig`, and `ReflectionConfig`; the
example below follows the official API shape:

```python
from gepa.optimize_anything import (
    EngineConfig,
    GEPAConfig,
    ReflectionConfig,
    optimize_anything,
)

from hcuopt.agent.gepa_demo import run_gepa_scripted_demo

config = GEPAConfig(
    engine=EngineConfig(max_metric_calls=4),
    reflection=ReflectionConfig(reflection_lm="<approved-litellm-model-id>"),
)
result = run_gepa_scripted_demo(
    seed_policy=seed_policy,
    cases=scripted_cases,
    case_evaluator=run_existing_scripted_m2b_and_read_d_evidence,
    optimize_anything=optimize_anything,
    config=config,
    max_metric_calls=4,
)
```

Do not connect the demo to a real HCU target. `max_metric_calls` must match the
wrapper cap; with `N` fixed cases, the hard maximum is `max_metric_calls * N`
Generation Runs. Set a model-side cost/timeout limit separately. GEPA's optional
tracking integrations remain disabled for this pilot so candidate text and
evidence stay in the project's configured stores.

## What the demo must report

- Seed and selected policy SHA-256.
- GEPA version, reflection model identity, metric-call budget and actual calls.
- For every policy/case: Run ID, Request/Plan/Knowledge Hashes, D input digest,
  proposal yield, duplicate/rejection reasons and bounded budget usage.
- Baseline-policy versus evolved-policy results on the same Scripted cases.
- Fixed authority fields: synthetic/dev-only, not measured, no Formal intake,
  no automatic release.

After GEPA selects a policy, a human may choose one resulting Proposal and run
the already existing review, Candidate Package promotion, source-family
verification and M2a Scripted Intake steps. Those steps are outside GEPA's
fitness callback and are not implied by a high Scripted score.

## Current implementation boundary

`hcuopt.agent.gepa_demo` implements bounded orchestration, D Read Model binding,
scoring, and redacted feedback. Deployment-specific creation of immutable
Knowledge Snapshots and scripted Generation Runs is intentionally injected via
`case_evaluator`; the branch does not add a new generation API or invoke a model
or HCU from D. A runnable service adapter needs the approved Agent/Apex Scripted
profile and its existing Store/PostgreSQL configuration.
