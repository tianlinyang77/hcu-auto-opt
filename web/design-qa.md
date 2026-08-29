# UI-3 Product Design QA

## Comparison target

- Source visual truth: `qa/ui3-source-ui2-desktop-1440x1000.png` (approved UI-2 workspace chrome and visual system captured at the same viewport).
- Implementation: `qa/ui3-implementation-desktop-candidate-pass1.png`.
- Combined full-view comparison: `qa/ui3-comparison-desktop.png`.
- Desktop viewport and captured pixels: 1440 × 1000 CSS px at device scale 1; source and implementation are both 1440 × 1000 pixels.
- Mobile evidence: `qa/ui3-implementation-mobile-correctness-pass1.png` and `qa/ui3-implementation-mobile-correctness-detail-pass1.png`.
- Mobile viewport and captured pixels: 390 × 844 CSS px at device scale 1; both captures are 390 × 844 pixels.
- Focused Build failure evidence: `qa/ui3-implementation-desktop-build-failed-pass1.png`.
- Focused Correctness lock evidence: `qa/ui3-implementation-desktop-correctness-locked-pass1.png`.
- Explicit API-error evidence: `qa/ui3-implementation-api-error.png`.
- State: explicit Synthetic demo with three frozen candidates, two immutable Build Artifacts, one immutable Build failure, and Correctness intentionally unavailable until Search Barrier authority exists.

The source and implementation are adjacent workflow slices, so the comparison verifies design-system continuity rather than pixel-identical content. Both artifacts use the same desktop viewport, theme, modal anatomy, typography, density, borders, radii, icon family, semantic colors, and trust-boundary treatment.

## Findings

No actionable P0, P1, or P2 findings remain.

- Fonts and typography: Noto Sans SC Variable remains the Chinese/interface font, with JetBrains Mono Variable reserved for IDs, Hashes, timestamps, codes, versions, and machine states. The 8–13 px evidence text, heading weights, line heights, truncation, and mixed Chinese/English hierarchy remain consistent with UI-2.
- Spacing and layout rhythm: the implementation reuses the approved full-screen workspace, 1 px borders, 7–12 px radii, compact 8–20 px spacing, summary strip, three-step evidence rail, and two-column desktop body. At 390 px it collapses to one column with one internal vertical scroll region and no horizontal overflow.
- Colors and visual tokens: existing dark surfaces, cyan Authority labels, mint completed states, amber locked boundary, purple Synthetic badge, and red failure/API-error states are reused without adding a second palette. Text and Phosphor icons reinforce every semantic color.
- Image and asset quality: no new raster artwork was required. The existing product mark stays unchanged behind the modal, and all visible workflow symbols use the existing Phosphor family. No inline SVG, emoji, CSS illustration, gradient, or placeholder asset was introduced.
- Copy and content: the workspace explains Source Package lineage, Candidate/Baseline/Manifest Hashes, replacement point, optimization intent, Artifact or immutable Build failure, and the exact reason Correctness is locked. It explicitly states that Candidate state is not evidence and keeps `formal_signoff_allowed=false` and automatic release `false` visible.
- Accessibility and behavior: the workspace is an ARIA modal with named stage buttons, listbox/options, semantic headings and description lists, labelled close/retry/export controls, focus-visible styles, reduced-motion support, visible loading/empty/error/retry states, and body scroll locking.

## Full-view and focused evidence

- Full-view comparison: `qa/ui3-comparison-desktop.png` shows the approved UI-2 header, status color language, card density, typography, footer, and trust-boundary treatment carried into UI-3 at the same 1440 × 1000 viewport.
- Build focus: `qa/ui3-implementation-desktop-build-failed-pass1.png` confirms that a failure code and immutable failure Hash replace the Artifact without ambiguous mixed success/failure signals.
- Correctness focus: `qa/ui3-implementation-desktop-correctness-locked-pass1.png` confirms that a Build failure does not become a Correctness failure and that the missing Search Barrier remains visibly locked.
- Responsive focus: `qa/ui3-implementation-mobile-correctness-pass1.png` confirms the 2 × 2 summary strip, compact stage rail, candidate list, and fixed footer at 390 × 844. `qa/ui3-implementation-mobile-correctness-detail-pass1.png` confirms that the evidence detail and safety boundary remain reachable through the single internal scroll region.
- Failure focus: `qa/ui3-implementation-api-error.png` confirms that the original GET failure remains visible, retry is the only primary action, and no Synthetic Authority is silently substituted.

## Comparison history

### Pass 1 — passed

- No P0/P1/P2 visual mismatch was found in the same-viewport full comparison.
- Candidate selection, Candidate/Build/Correctness stage switching, the Build-failure branch, the Correctness-locked branch, mobile scrolling, explicit GET failure, retry recovery, sidebar direct entry, and workspace close/reopen all worked.
- No visual fix was required, so a second comparison loop was not necessary.
- Browser console warnings/errors after a clean reload and normal interaction flow: none. The forced 404 used only to capture the explicit API-error state was expected and recovered after the fixture was restored.

## Primary interactions tested

- Open UI-3 from the dashboard `查看候选证据` action.
- Open the same workspace directly from `候选管理`, `构建中心`, and `正确性评估` navigation, preserving the requested initial stage.
- Switch among Candidate, Build, and Correctness stages.
- Select a built candidate and a Build-failed candidate and verify their evidence remains separate.
- Verify that a built candidate says `等待 Search Barrier 写入权威证据` rather than inferring correctness from state.
- Remove the Synthetic GET fixture temporarily, verify the explicit API error, restore it, and recover through `重新读取`.
- Repeat the Correctness flow at 390 × 844 and verify a single scroll region without horizontal overflow.
- Check browser console warnings and errors after clean reload and normal interactions.

## Follow-up polish

- P3: when a future Formal Candidate carries a much longer optimization intent, consider an optional expanded detail line in the left candidate list. The current truncation is appropriate for the frozen 2–4 Scripted family.

final result: passed

---

# UI-4 Evaluation Evidence implementation QA

## Scope and authority boundary

- Implementation: `src/EvaluationEvidenceWorkspace.jsx` with four read-only stages: Search, Holdout, FWER, and Evidence.
- Data source: `GET /v1/operator/search-rounds/{round_id}/evaluation-evidence` or the explicitly labelled Synthetic demo fixture.
- Authority rule: the frontend displays backend-authored Barrier, Reveal, Multiple Comparison, and EvidenceBundle records. It does not calculate means, confidence intervals, MDE, FWER, verdicts, recommendations, or speedup.
- Release boundary: `synthetic=true`, `real_performance_claim_allowed=false`, `formal_signoff_allowed=false`, and `automatic_release_allowed=false` remain visible and immutable in this slice.
- Visual direction: UI-4 extends the approved UI-3 workspace chrome, Noto Sans SC / JetBrains Mono typography, Phosphor icons, compact evidence density, and the existing dark semantic palette.

## Automated findings

No actionable implementation P0, P1, or P2 finding remains in the automated QA scope.

- Search: renders the frozen family, promoted members, immutable receipt/evidence hashes, restart effects, statistics-valid state, and failure evidence without recomputing a ranking.
- Holdout: separates Commit/Reveal authority from the Holdout Barrier and exposes lease, resource, fencing token, family hash, and cleanup evidence.
- FWER: renders the backend-authored Bonferroni protocol, family alpha, candidate alpha, adjusted intervals, MDE fields, verdict, and recommendation without deriving a new conclusion in JavaScript.
- Evidence: renders the complete authority chain and Evidence Index while keeping Synthetic conclusions separate from Formal signoff and release.
- Failure behavior: unavailable or identity-drifted authority fails closed in the backend; loading, empty, and explicit API-error states do not silently fall back to demo data.
- Accessibility structure: the workspace keeps the existing ARIA modal pattern, named stage controls, semantic headings and description lists, visible focus treatment, and reduced-motion support.
- Responsive structure: UI-4 reuses the established single internal workspace scroll region and responsive breakpoints instead of introducing a second page-level scroll model.

## Validation evidence

- Backend focused authority and fixture tests: 30 passed.
- Frontend unit tests: 12 passed, including evaluation-stage availability and the no-recomputation verdict boundary.
- Ruff and ESLint: passed.
- Production/Sites build: passed and produced `dist/client/index.html`, `dist/server/index.js`, and `dist/.openai/hosting.json`.

## Browser QA status

No new screenshot, DOM inspection, viewport comparison, or interactive browser test was performed for UI-4 because it was not requested in this iteration. The approved UI-3 browser evidence above remains the visual-system baseline; this section therefore records implementation and automated QA only and does not claim a new pixel-level visual verdict.

final result: automated implementation QA passed; browser visual QA not requested
