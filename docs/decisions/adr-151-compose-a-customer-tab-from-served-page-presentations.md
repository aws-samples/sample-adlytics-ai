# ADR-151: Compose a customer tab from served page presentations
Date: 2026-10-22
Status: Proposed (2026-10-22). Implemented on `feat/ui-refresh` for the Overview › Growth check at `/`
(UI refresh slice 2.1, readout OD-UX-3). Not deployed yet. Amends
[ADR-115](adr-115-organise-the-app-into-customer-question-hubs-from-one-navigation-file.md): the
navigation file gains a third tab kind beside v2 page tabs and operator static routes.
## Context
The readout's Growth check (new-views rank 1, Appendix A.1) answers "is my advertising growing my
business?" on one screen, from figures four pages already serve: the 7-day Scorecard, Retail health,
New customers and Campaigns presentations. A v2 page entry needs its own artifact, baker, roster row
and bake (`presentation/pages/v2/pages.yaml`; an artifact-served page with no measurement batch fails
`make check-bakeability`). Dev runs GRAPH mode, where no repository-owned command requests a re-bake and
the deploy-time backfill sends nothing (`docs/ui/preset-artifact-publisher.md`, "First bake and
recovery"), so a new artifact would not reach dev before the customer demo on 2026-10-23.
## Decision
1. **A composed tab kind.** `presentation/navigation.yaml` lists `composed_routes`, each with `route,
   hub_id, tab_id, tab_label, tab_description, order, sources`. The generator
   (`src/adapters/jobs/presentation_validation/navigation.py`) refuses a composed tab on an operator
   hub, a source that isn't a routed v2 page tab, and an entitlement. The route manifest carries the
   tab with `kind: composed`; the frontend decoder accepts only a composed tab it renders.
2. **No artifact, baker or API route of its own.** The browser requests each source's existing served
   presentation and renders each band from one source. A band whose source fails or is absent shows
   that source's own served or catalog absence (`docs/ui/page-absence-copy.md`, ABS-OV-1) and never
   blocks another band.
3. **Every figure is its drill-down's figure.** A composed tab adds, ranks or recomputes nothing. Each
   band links to its source tab on the same window.
4. **The Overview owns `/`.** `_root_tab_id` counts composed tabs at `/`; exactly one tab owns the
   root, so no v2 page entry may carry `/` in `compatibility_routes` (the pins in `page_schemas.py`,
   `bindings.py` and `frontend/lib/page-bindings.ts`).
## Amendment, 2026-10-22 (UI refresh 3.7)

A second composed tab, Retail health › Products and ads at `/products-and-ads/` (sources
`[retail-health]`), reads the served Retail health parent and, for each row, its baked product detail
(ADR-125) over the row's own capability, at most four in flight. It keeps Retail health's window
control. A detail is used only when it answers for the parent's revision, window and product; any
other answer, or a failed request, reads as that row's absence. The route allowlist gains the route
through the navigation generator, as for `/`. Composed tabs may also carry an optional `question`
(UI refresh 3.8).

## Consequences
- The Growth check ships with no bake, and its figures stay equal to the Scorecard, Retail health, New
  customers and Campaigns tabs by construction.
- Four requests per visit instead of one. Each is a served artifact read.
- The page context the assistant reads is the Scorecard's served context; a dedicated Overview context
  would need a baked artifact.
- A later `GrowthOverviewArtifact` (readout design L) can replace the composition without a route
  change. Products and ads (readout A.10) can reuse the kind.
