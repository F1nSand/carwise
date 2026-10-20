# Homepage atlas design check

Reviewed `PRODUCT.md`, `DESIGN.md`, `frontend/src/App.vue`, `frontend/src/components/layout/AppShell.vue`, `frontend/src/components/home/AnalysisLanding.vue`, `HomeAnalysisPreview.vue`, `QueryComposer.vue`, `HomeMarketLens.vue`, and `HomeCapabilities.vue`, plus the hero asset provenance. The page extends the existing “汽车编辑档案” world; it does not establish a new global visual system.

## Observed rules and consistency

- **Palette and surface:** the hero photograph and forest-green overlay copy sit above a warm-paper page; query entry uses a subtly differentiated paper surface, while market and capability sections return to open paper with fine dividers. Forest green indicates the primary action and selected budget, with restrained secondary colors. This carries forward the existing palette and flat editorial surfaces.
- **Hero and hierarchy:** the full-width hero uses a two-line white title (up to 78px), short explanatory copy, and a clear CTA, followed by a caption row with live record coverage. The image has explicit illustrative/non-listing disclosure and provenance identifying an AI-generated visual; this follows Evidence First and the PRODUCT data constraints. The 78px hero scale is a local expression, not a replacement for the global type token.
- **Query composer:** a warm-paper form follows the hero, pairing a clear prompt with live capability/limitation details, real-data helper text, and actionable examples. The textarea remains an unboxed editorial field; the forest-green submit control and keyboard focus states align with incumbent button and field rules.
- **Market lens:** budget selectors default to 10—20 万 and request the actual price distribution for the active data version. The view reports median and sample counts, labels equal-width bins, distinguishes empty/error/loading states, and offers a data table. Green highlights the selected range/median bin; chart marks are explanatory encodings rather than decorative market claims.
- **Capabilities and responsive behavior:** supported conditions are rendered as editorial rows that seed the query; unsupported conditions remain explicit. Content stacks on narrow screens, the chart scrolls horizontally where needed, and reduced-motion preference bypasses reveal motion. The homepage-only AppShell mode removes the constrained content width and padding while preserving the shared top bar and its existing offset.

## Drift retained

The existing `DESIGN.md` still calls the shared navigation sticky although `TopBar.vue` is fixed (previously recorded), and its homepage display range tops out at 70px while this page’s local hero reaches 78px. This pass records the local specialization and leaves both documentation updates for a deliberate refresh; the earlier sidecar navigation drift also remains unmodified.
