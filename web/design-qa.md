# UI-1 Product Design QA

## Comparison target

- Source visual truth: `qa/ui1-source-dashboard.png`
- Implementation: `qa/ui1-implementation-preview-final.png`
- Combined comparison: `qa/ui1-comparison-desktop.png`
- Desktop viewport: 1280 × 720 CSS px, browser DPR 1.5
- Source pixels: 1265 × 712
- Implementation pixels: 1265 × 712
- Mobile evidence: `qa/ui1-implementation-mobile.png`, `qa/ui1-implementation-mobile-candidates.png`, `qa/ui1-implementation-mobile-preview.png`
- Mobile viewport: 390 × 844 CSS px; captured content pixels: 375 × 755 because the browser chrome and scrollbar are excluded
- State: Synthetic demo, pass Preview; blocked, expired, and API-error fixtures were also exercised
- Normalization: source and implementation were captured in the same in-app browser, theme, desktop viewport, CSS scale, and browser session. UI-0 is the selected visual system rather than a pixel-identical screen, so the comparison evaluates design-system continuity and core workflow quality.

## Findings

No actionable P0, P1, or P2 findings remain.

- Fonts and typography: Noto Sans SC Variable remains the interface font; JetBrains Mono Variable remains limited to IDs, Hashes, timestamps, codes, and machine states. Heading weights and small-copy density match UI-0. Stable Preflight codes now have Chinese titles while the original backend message remains visible as evidence.
- Spacing and layout rhythm: the 1280 px desktop view keeps the UI-0 1 px borders, compact 6–12 px radii, dense cards, and 18–22 px section spacing. The wizard uses the same surface hierarchy instead of introducing a second visual language.
- Colors and tokens: the existing `--bg`, `--surface`, `--border`, cyan, mint, amber, red, and purple tokens are reused. Pass, block, expiry, Synthetic, and automatic-release-disabled states are distinguishable by icon and text as well as color.
- Image and asset quality: the product mark is unchanged. All workflow controls use the existing Phosphor icon family; no inline SVG, CSS illustration, emoji, or placeholder asset was introduced.
- Copy and content: the flow says what is selected, what the backend resolved, why a Preview is blocked, and that UI-1 does not call `:start`. `automatic_release_allowed=false` remains persistent.
- Accessibility and behavior: semantic buttons, selects, radio state, pressed state, focus-visible outlines, reduced-motion handling, and 44 px mobile primary controls are present. Desktop and mobile core interactions completed without horizontal overflow.

## Focused evidence

- Target/Profile/Hotspot selection: `qa/ui1-implementation-scope.png` confirmed labels, exact version selection, Hotspot authority, shape/dtype, and Candidate count.
- Candidate Family and budget: `qa/ui1-implementation-mobile-candidates.png` confirmed 2–4 selection affordance, selected state, reviewer identity, immutable package Hash, read-only budget, and a visible primary action at narrow width.
- Preview and trust boundary: `qa/ui1-implementation-preview-final.png` confirmed Chinese Preflight titles, raw backend messages, Resolved Plan Hashes, expiry, `automatic_release_allowed=false`, and the explicit no-Start boundary.
- Failure states: `qa/ui1-implementation-preview-blocked.png` plus browser assertions confirmed block; browser assertions also confirmed `Preview 已过期` and `计划预览失败` states.

## Comparison history

### Pass 1 — blocked

- [P2] Step transitions preserved the internal scroll offset, so the Preview status banner could open above the visible area.
  - Fix: added a workspace-body ref and reset its scroll position whenever the wizard step changes.
- [P2] Candidate and Preview primary actions could fall below the 720 px desktop fold.
  - Fix: made the full-width action row sticky inside the scrollable workspace and changed it to an opaque surface so underlying evidence does not visually leak through.
- [P3] Raw English Preflight messages were technically correct but slowed Chinese scanning.
  - Fix: added stable Chinese titles keyed by backend check code and retained the raw backend message below each title.

### Pass 2 — passed

- Re-captured desktop pass, blocked, mobile selection, and mobile Preview states.
- Verified the status banner starts at the top after each transition.
- Verified primary actions stay reachable without hiding the persistent trust-boundary footer.
- Browser console warning/error result: none.
- No remaining P0/P1/P2 visual or interaction finding.

## Primary interactions tested

- Open from dashboard CTA, sidebar, and top context affordance.
- Select registered Target, Workload, Measurement Profile, and Hotspot.
- Select/deselect Candidate Package inputs and enforce the 2–4 bound.
- Generate pass, block, expired, and error Preview states.
- Read Resolved Plan, budget, expiry, hashes, and release boundary.
- Complete the same pass flow at 390 × 844 without horizontal overflow.

## Follow-up polish

- P3: when multiple production Profiles are registered, consider adding search inside the native selects. This is not needed for the current single Scripted catalog.

final result: passed
