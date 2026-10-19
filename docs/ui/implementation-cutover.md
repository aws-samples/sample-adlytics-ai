# Workspace implementation cutover

| | |
|---|---|
| **Status** | Approved implementation strategy — manifest revision `2026-09-08.1` |
| **Owns** | Clean replacement, migration, and retirement rules for frontend screens and styling |
| **Parent** | [Decision Workspace UI](README.md) |

## 1. Decision

The Decision Workspace is a fresh implementation. It must not become a second visual system layered over the current generic screen hierarchy.

The implementation builds a new workspace shell, page composition layer, visual-evidence primitives, temporal-control contract, theme-preference contract, and assistant presentation from the approved specifications. When each replacement route passes contract/browser/accessibility checks, the superseded screen component and obsolete CSS are deleted. The final production tree has one shell, one primitive vocabulary, and one page implementation per route.

### V2-only authorization

There are no V1 users or deployed compatibility consumers. The Decision Workspace therefore has no V1 retention window: dual request/response transport, raw-date controller state, V1-only routes, generic legacy fallback, and V1 declarations may be retired as soon as their dependency-closed V2 replacement is implemented and validated. This authorization does not permit deleting a route, custody boundary, or accessibility behavior before its V2 successor is present; it removes compatibility as a deletion reason, not correctness validation.

## 2. Replacement boundaries

| New boundary | Replaces | Cutover rule |
|---|---|---|
| Workspace shell | Existing generic shell/header/sidebar styling | Preserve session, route, skip-link, and assistant behavior; replace presentation markup/CSS as one unit. |
| Review-page composition | Generic `PageContent` section/card renderer | New pages receive only typed artifacts and generated actions; no fallback to legacy generic metric layouts. |
| Campaign Portfolio and Detail | Existing list/detail screen composition | Keep opaque capability custody, add artifact-backed temporal/visual evidence, replace visual composition, then delete legacy campaign view styling. |
| Product portfolio and detail | Existing list/detail screen composition | Keep opaque capability custody; accept each visual only from returned typed series, then delete legacy product view styling. |
| Evidence primitives | Duplicated card/table/state/chart CSS generations | Translate semantic primitives into the new token/component system; delete superseded selectors after callers migrate. |
| Assistant inspector | Existing utility-styled chat presentation | Preserve conversation/session/request behavior; add ordered streaming prose and bounded evidence cards without passing page context. |

### 2.1 Route and component cutover inventory

This inventory is the verified pre-replacement record for removal. Every listed source path, export, selector family, and test was inspected in Task 1; no row is eligible for removal before the replacement’s recorded acceptance is read.

| Inventory ID | Verified current entry, retained behavior, and evidence | Replacement and Task 23 deletion scope | Required implementation evidence | Retirement state |
|---|---|---|---|---|
| `CUT-SHELL-01` | `frontend/app/layout.tsx` → `ApplicationRoot`; `frontend/features/PresentationApp.tsx` exports `ApplicationRoot`/`PresentationApp`; `frontend/components/AppShell.tsx` exports `AppShell` and retains navigation, skip link, route-heading focus, sign-out, persistent `ChatPanel`, and two-state `adlytics.theme`. CSS: `frontend/app/globals.css` `[data-theme]`, `.shell`, `.sidebar`, `.topbar`, `.skip-link`, `.primary-nav`, `.nav-link`, assistant/layout families. Tests: `frontend/test/ui.test.tsx`, `frontend/e2e/static-export.spec.ts`. | Shared System/Light/Dark shell and assistant inspector, revision `2026-09-08.1`. Remove only the superseded `AppShell` implementation and its identified two-state theme/chrome selectors after all consumers migrate; retain session, skip-link, route-focus, and assistant behavior. | Typed shell state, local-only preference, keyboard/focus restoration, opaque fallback, all layouts, assistant continuity, browser journey. | Inventoried; replacement acceptance pending |
| `CUT-CAMPAIGN-01` | Legacy `/campaign-detail/`: `frontend/app/campaign-detail/page.tsx` → `PresentationApp route="campaign-detail"`; `PresentationController` campaign list/redemption branch; `frontend/components/CampaignAttributionView.tsx`. CSS: shared campaign/card/entity/chart selectors in `frontend/app/globals.css`. Tests: `frontend/test/campaign-attribution.test.ts`, `frontend/test/campaign-attribution-fixture.ts`, `frontend/test/ui.test.tsx`, `frontend/e2e/static-export.spec.ts`. Retain opaque selection, expiry/signature refresh. | [Campaign Portfolio](campaign-portfolio/README.md), revision `2026-09-08.1`, at `/campaigns/`. Remove the legacy route component, CampaignAttributionView, campaign-only fixtures/tests, and exclusively owned selectors only after `/campaigns/` accepts; keep `/campaign-detail/` only as the documented redirect/alias. | V2 artifact/capability flow, temporal control, strict decode, exact data, fallback, non-production route acceptance. | Inventoried; replacement acceptance pending |
| `CUT-CHANNEL-01` | `frontend/app/channel-performance/page.tsx` → `PresentationApp`; `PageContent` dispatches `channel_attribution_robustness` to `ChannelAttributionRobustnessView`. CSS: shared card/table/chart/evidence families in `globals.css`. Tests: `frontend/test/ui.test.tsx` and channel parent/child flow in `frontend/e2e/static-export.spec.ts`; no separate channel component test exists. Retain parent/child opaque row redemption and refresh behavior. | Channel Robustness, revision `2026-09-08.1`. Remove only the legacy PageContent dispatch and page-specific legacy view/CSS/tests after its specialized route accepts; retain custody behavior through the successor. | Parent/child temporal binding, expiry refresh, browser accessibility, non-production route result. | Inventoried; replacement acceptance pending |
| `CUT-PRODUCT-01` | `frontend/app/retail-detail/page.tsx` → `PresentationApp`; controller list/redemption/Boolean comparison branch; `PageContent` dispatches to `RetailProductView`. CSS: shared card/entity/table/evidence families. Tests: `frontend/test/retail-product-fixture.ts`, `frontend/test/ui.test.tsx`, `frontend/e2e/static-export.spec.ts`. Retain opaque selection and server-only comparison dates. | Product Evidence, revision `2026-09-08.1`. Remove the unparented legacy product branch, legacy view dispatch, exclusively owned fixtures/tests, and selectors only after the V2 parent-bound flow accepts. | V2 visual transport, split evidence, exact data, non-trend state, non-production route result. | Inventoried; replacement acceptance pending |
| `CUT-MARKETPLACE-01` | `frontend/app/workspace/page.tsx` → `PresentationApp`; `PageContent` dispatches `marketplace_comparison` to `MarketplaceComparisonView`. CSS: shared table/card/evidence selectors. Tests: `frontend/test/marketplace-comparison.test.tsx`, `frontend/test/ui.test.tsx`, `frontend/e2e/static-export.spec.ts`. Retain table-only native-currency/source-time disclosure. | Marketplace Movement, revision `2026-09-08.1`. Remove only the legacy PageContent dispatch and exclusively owned legacy view/CSS/tests after V2 transport route acceptance. | Temporal transport, four outcomes, native-currency/source-time custody, browser and non-production evidence. | Inventoried; replacement acceptance pending |
| `CUT-HEALTH-01` | `frontend/app/data-health/page.tsx` → `PresentationApp`; `PageContent` dispatches `data_health_detail` to `DataHealthDetailView`. CSS: shared card/table/evidence selectors. Tests: `frontend/test/data-health-detail.test.tsx`, `frontend/test/data-health-fixture.ts`, `frontend/test/ui.test.tsx`, `frontend/e2e/static-export.spec.ts`. Retain ordered checks, provenance, and recovery semantics. | Evidence Health, revision `2026-09-08.1`. Remove only the legacy PageContent dispatch and exclusively owned legacy view/CSS/tests after successor acceptance. | Temporal/transport state split, two checks, exact-source disclosure, browser and non-production evidence. | Inventoried; replacement acceptance pending |
| `CUT-MEDIA-01` | No existing route module, generated binding, controller branch, component, fixture, or test. No deletion scope exists. | Media Efficiency, revision `2026-09-08.1`. Add a new implementation; no existing file may be retired for this row. | Parent scope list, opaque scope/window redemption, child artifact, typed audience absence, exact data, fallback, route acceptance. | Inventoried; new implementation pending |
| `CUT-PRIMITIVES-01` | `frontend/components/PageContent.tsx` exports generic `EntityPicker`, `TrendChart`, `MeasurementView`, and fallback rendering; `frontend/components/PrecisionPrimitives.tsx` exports outcome/table/state/action primitives; `frontend/components/ExactChart.tsx` exports `ExactChart`. CSS: `globals.css` `.card`, `.section`, `.measurement-grid`, `.metric*`, table, chart, state, and responsive families. Tests: `frontend/test/precision-primitives.test.tsx`, `frontend/test/ui.test.tsx`, `frontend/test/data-health-detail.test.tsx`, `frontend/test/marketplace-comparison.test.tsx`. Retain accessibility and exact-data semantics. | Typed evidence primitives and shared tokens, revision `2026-09-08.1`. Remove generic canonical-route fallback, duplicate selectors, and obsolete primitive tests only after every consumer uses its specialized typed view and all routes reaccept. | One primitive vocabulary, opaque evidence surfaces, responsive exact-data alternatives, consumer migration evidence. | Inventoried; replacement acceptance pending |

Task 23 may remove only the exact scopes listed above and only after the matching replacement evidence is read. No inventory row authorizes a deletion, rename, feature flag, or hidden legacy route in Task 1.

## 3. No-legacy rules

- Do not leave hidden legacy routes, duplicate component trees, or CSS overrides as fallback presentation.
- Do not introduce a feature flag that chooses between old and new screens after the replacement is accepted.
- Do not copy mockup markup into production. Reimplement its approved decisions through typed React components.
- Do not retain generic browser metric rendering for a page that has an approved typed artifact.
- Do not delete a backend artifact/API, selection capability, or accessibility behavior until its replacement has passed contract and browser validation.

## 4. Delivery sequence

1. Establish one production token, accessibility, visual-evidence, temporal-control, theme-preference, and restrained-glass component contract.
2. Build the new shell and persistent assistant inspector, including System/Light/Dark preference, streaming prose, Cancel, and bounded public evidence-card projection.
3. Build the Campaign Portfolio → Campaign Detail reference route and its artifact/API extension.
4. Apply the same composition system to Product Evidence, Channel Robustness, Marketplace Movement, and Evidence Health.
5. Add Media Efficiency only after its required server-owned artifact, visual-series, and selection contracts exist.
6. Remove superseded frontend screens, old component styling, and obsolete tests in the same cutover change.

## 5. Acceptance

A route can retire its old screen only after its matching `CUT-*` inventory row records the verified legacy component/CSS/test/deletion scope and the replacement proves the matching Design revision, typed decoding, exact evidence/qualification rendering, server-resolved temporal selections, accessible chart-data alternative, generated actions, assistant availability, keyboard use, System/Light/Dark selection with opaque glass fallback, wide/regular/compact layouts, and the corresponding browser journey. Non-production route acceptance is read evidence, not a successful deploy or local test substitute.
