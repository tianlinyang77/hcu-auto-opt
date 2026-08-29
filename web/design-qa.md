# UI-2 Product Design QA

## Comparison target

- Source visual truth: `qa/ui1-implementation-preview-final.png` (the approved UI-1 workspace chrome and visual system).
- Implementation: `qa/ui2-implementation-desktop-pass2.png`.
- Combined full-view comparison: `qa/ui2-comparison-desktop.png`.
- Desktop viewport: 1440 × 1000 CSS px, in-app browser; captured implementation pixels: 1440 × 799 because browser chrome is excluded.
- Source pixels: 1265 × 712. For the combined comparison, the source was proportionally normalized to 1420 × 799 and placed beside the 1440 × 799 implementation with a 16 px divider.
- Mobile evidence: `qa/ui2-implementation-mobile-pass2.png`.
- Mobile viewport: 390 × 844 CSS px; captured content pixels: 390 × 785 because browser chrome is excluded.
- API-error evidence: `qa/ui2-implementation-api-error.png`.
- State: explicit Synthetic demo, finalized durable StartIntent with three bound Candidate members; API 404 and retry recovery were also exercised.

The source and implementation are different workflow states, so this is a design-system continuity comparison rather than a claim of pixel-identical content. The same desktop theme, workspace anatomy, typography, density, borders, radii, icons, status colors, and trust-boundary treatment are compared.

## Findings

No actionable P0, P1, or P2 findings remain.

- Fonts and typography: Noto Sans SC Variable remains the Chinese/interface font. JetBrains Mono Variable is limited to IDs, hashes, timestamps, codes, versions, and machine states. Heading weight, 9–12 px evidence copy, line height, truncation, and mixed Chinese/English hierarchy match the approved UI-1 density.
- Spacing and layout rhythm: the implementation keeps the same modal frame, 1 px borders, 7–12 px radii, 12–20 px gaps, compact status banner, two-column desktop evidence grid, and scroll-contained body. The 390 px view collapses to one column without horizontal overflow.
- Colors and visual tokens: the existing dark surfaces, cyan Authority labels, mint completed states, amber locked boundary, purple Synthetic badge, and red API-error state are reused. State meaning is reinforced with text and Phosphor icons rather than color alone.
- Image and asset quality: no new raster artwork was required. The existing product mark remains unchanged behind the modal, and all visible workflow symbols use the existing Phosphor icon family. No inline SVG, emoji, CSS illustration, or placeholder asset was introduced.
- Copy and content: the screen explains the durable Start sequence, actor/idempotency identity, Plan Authority, Candidate binding, backend safe error, and immutable release boundary. It explicitly says that UI-2 does not call Start, Reconcile, Cancel, or Signoff and keeps `automatic_release_allowed=false` visible.
- Accessibility and behavior: the workspace is an ARIA modal with a named heading, semantic buttons and description lists, focus-visible styles inherited from UI-0/UI-1, reduced-motion support, visible loading/error/retry states, and a labelled close control. The background document is scroll-locked while the workspace is open.

## Full-view and focused evidence

- Full-view comparison: `qa/ui2-comparison-desktop.png` confirms that the approved UI-1 workspace header, progress rail, status banner, evidence cards, compact typography, and trust-state colors carry into UI-2 without a second visual language.
- Authority and Candidate binding details remain legible in the original 2876 × 799 combined image, so a separate desktop crop was not needed. The full-resolution evidence shows hash truncation, two-column labels, member binding states, and 1 px card boundaries.
- Responsive focus: `qa/ui2-implementation-mobile-pass2.png` confirms the five-step rail, finalized summary, two-column fact pairs, truncated long Authority strings, one internal scroll region, and no horizontal overflow at 390 px.
- Failure focus: `qa/ui2-implementation-api-error.png` confirms that the backend error code/message remain visible, no demo fallback is substituted, and retry is the only primary action.

## Comparison history

### Pass 1 — blocked

- [P2] The mobile workspace initially exposed both the page scrollbar and the modal body scrollbar, reducing usable width to roughly 375 px and making the audit feel nested inside the dashboard.
  - Fix: the StartIntent workspace now locks background body scrolling for its lifetime and restores it on close.
- Validation after the fix showed the dialog using the full 390 px viewport with one internal scroll region.

### Pass 2 — passed

- Re-captured desktop and mobile finalized states after scroll locking.
- Re-captured the explicit API-error state by making the Synthetic fixture unavailable, restored the fixture, and verified that `重新读取` returned to the finalized Authority.
- Browser console warning/error result after finalized, close/reopen, error, and retry flows: none.
- No remaining P0/P1/P2 visual or interaction finding.

## Primary interactions tested

- Open the current Round's StartIntent from the dashboard CTA.
- Close the workspace and reopen it from the `启动审计` sidebar entry.
- Read the five durable Start steps, immutable identifiers, Plan Authority, service identity, and three Candidate member bindings.
- Force a GET failure, verify the original safe error code/message, restore the Authority, and recover with `重新读取`.
- Repeat the finalized view at 390 × 844 and verify a single scroll region without horizontal overflow.
- Check browser console warnings and errors after all interactions.

## Follow-up polish

- P3: if future Formal StartIntents contain four candidates with longer optimization descriptions, consider a compact/expanded member-row toggle. It is not needed for the current 2–4 member Scripted scope.

final result: passed
