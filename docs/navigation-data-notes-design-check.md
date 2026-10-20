# Navigation and data disclosure design check

Reviewed the shipped styles and components against `PRODUCT.md`, `DESIGN.md`, and `.impeccable/design.json`. This note records observed rules and consistency evidence for the fixed navigation and redesigned data disclosure.

## Observed system

- Palette: warm paper canvas and white surfaces use ink and muted ink for reading; forest green marks primary actions, active navigation, and coverage bars; terracotta marks caution and limits. These roles match the token palette and Evidence First / Data Color rules.
- Type: the Chinese system sans stack carries a compact ramp from 11–14px labels and body through 21–28px section titles to responsive 34–58px page titles; large record counts use the same restrained sans voice. This follows Quiet Hierarchy.
- Layout: the shared top bar is fixed at 76px, becoming a 98px two-row bar at 760px; `AppShell` offsets page content by the token height. `scrollbar-gutter: stable` keeps content width steady, and the nav uses an aligned three-column shell with a fixed four-column link grid.
- Data disclosure: a large forest-green, live-data overview leads into field coverage bars, supported and unsupported filters, a numbered method, and explicit limits/FAQ. The page index and anchor scroll margins respect the fixed bar; mobile collapses the index and content into one column.
- Surface rules: sections rely on open space, fine borders, and restrained color blocks rather than stacked cards; data values come from the capability response, and coverage labels explain their limits. This matches Evidence First, Border Before Shadow, and the documented flat surface language.

## Documentation drift

`DESIGN.md` still describes global navigation as sticky, while `TopBar.vue` is `position: fixed` and `AppShell.vue` applies the matching top offset. `.impeccable/design.json` also models the navigation as a static sticky-style sample without the fixed positioning or app offset; its nav pattern therefore drifts from the shipped shell. These are recorded here and left for a deliberate documentation refresh.
