# UI-0 Product Design QA

## Comparison Target

- Source visual truth: `C:\Users\17920\.codex\generated_images\01a04139-9607-7153-ba38-959c74258e17\exec-76855afd-c71a-49d3-9dab-ee2c4ca2dc4c.png`
- Review copy: `qa/source-visual.jpg`
- Browser-rendered implementation: `qa/implementation-desktop-pass2.png`
- Desktop full-view comparison: `qa/comparison-desktop-pass2.jpg`
- Desktop focused comparison: `qa/comparison-desktop-focus-pass2.jpg`
- Mobile before/after evidence: `qa/implementation-mobile-pass1.png`, `qa/implementation-mobile-pass2.png`
- Mobile navigation/dialog evidence: `qa/implementation-mobile-nav-pass2.png`, `qa/implementation-mobile-dialog-pass2.png`
- API unavailable state: `qa/implementation-api-error-pass2.png`

## Normalization

- Source pixels: 1487 x 1058.
- Desktop CSS viewport: 1488 x 1058; browser device pixel ratio: 1.5.
- Browser-rendered desktop screenshot: 1488 x 826. The in-app browser captured its visible surface, so the source was padded by one pixel on the right and cropped to the same top 1488 x 826 region before comparison.
- Mobile CSS viewport: 390 x 844; document client width: 375; browser-rendered screenshot: 375 x 755; device pixel ratio: 1.
- State: dark theme, `Synthetic demo`, Round v4, Build terminal 3/3, Evidence unavailable, Formal Signoff disabled, `automatic_release_allowed=false`.

## Full-view Comparison

`qa/comparison-desktop-pass2.jpg` places the normalized source on the left and the browser-rendered implementation on the right. The implementation preserves the selected direction's fixed header/sidebar, nine-step phase rail, build summary, Candidate Family table, evidence rail, and Agent/Apex boundary footer.

The implementation intentionally does not copy the source visual's fictitious 22.09% result, Human/Agent provenance claims, accepted evidence, or Apex readiness. Current authority is rendered instead. This is a trust-boundary correction, not design drift.

## Focused Comparison

`qa/comparison-desktop-focus-pass2.jpg` compares the phase rail, build summary, Candidate Family, and evidence rail at readable scale. A focused comparison was required because table typography and evidence copy were too small to judge reliably in the full-view composite.

## Required Fidelity Surfaces

- Fonts and typography: Noto Sans SC Variable is used for Chinese/UI text and JetBrains Mono Variable for IDs, hashes, timestamps, and machine states. Weight, wrapping, hierarchy, and truncation are stable on desktop and mobile. The 12px table treatment is intentionally denser than the visual target and is acceptable as P3 polish for a read-only operations view.
- Spacing and layout rhythm: desktop regions align with the visual target. Mobile phase and table overflow are contained locally; the document no longer scrolls horizontally. Borders, 6-8px radii, padding, and vertical rhythm are consistent.
- Colors and tokens: dark navy surfaces, cyan active state, mint success, amber locked state, red failure, and purple Synthetic state match the selected direction and retain readable contrast.
- Image quality and asset fidelity: the brand mark is a real raster asset; controls use one Phosphor icon family. There are no hand-built SVGs, emoji icons, CSS drawings, or placeholder image boxes.
- Copy and content: `Synthetic demo`, `不产生真实性能结论`, disabled signoff, false automatic release, and the Agent/Apex authority boundaries remain explicit. API failure never silently fabricates demo authority.

## Interaction and Accessibility Checks

- Candidate selection updates the read-only evidence panel.
- Blocking-reason, Agent unlock, and Apex responsibility dialogs open and close.
- Refresh preserves explicit demo authority and produces no console warnings/errors.
- API 404 shows a dedicated unavailable state; demo mode begins only after the user chooses it.
- Mobile navigation opens and closes; the blocking dialog stays inside the viewport.
- Visible mobile top-bar controls measure 44 x 44 CSS px.
- Focus-visible outlines, semantic buttons, table headings, dialog role, and reduced-motion rules are present.

## Comparison History

### Pass 1 — blocked

- [P1] Mobile document overflow. At a 390px viewport, document client width was 375px but scroll width was 821px. Candidate Family and Evidence inherited an 806px min-content grid track, so the whole page scrolled horizontally.
- [P2] Visible mobile header controls were 34 x 34px, below the intended touch target.

### Fixes

- Added `min-width: 0` to direct `dashboard-grid` children, preserving the 800px Candidate table as an internal scroll surface instead of expanding the document.
- Increased visible mobile icon buttons to 44 x 44px.

### Pass 2 — post-fix evidence

- Mobile document client width and scroll width are both 375px.
- Candidate table viewport is 345px with an internal 805px scroll surface.
- Desktop document client width and scroll width are both 1488px.
- Visible mobile menu and notification controls are 44 x 44px.
- Desktop and mobile browser console checks returned no warnings or errors.
- No actionable P0, P1, or P2 findings remain.

## Follow-up Polish

- [P3] If real Operator rows become substantially longer, revisit the 12px table type scale and column density with real content before increasing font size globally.

final result: passed
