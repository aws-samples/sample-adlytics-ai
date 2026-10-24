# Decision Workspace UI

| | |
|---|---|
| **Type** | Candidate implementation baseline for the Decision Workspace |
| **Status** | Approved for implementation at manifest revision `2026-10-09.2` ([`design-manifest.yaml`](design-manifest.yaml)). The UI refresh added the Overview › Growth check and Retail health › Products and ads composed tabs ([ADR-151](../decisions/adr-151-compose-a-customer-tab-from-served-page-presentations.md)) and the shared answer-sentence and ratio-display rules in §8. Owner ratification of revisions `2026-10-09.1` and `2026-10-09.2` is pending. Earlier revisions are in this file's history. |
| **Implementation disposition** | approved_for_implementation |
| **Implementation decision date** | 2026-10-07 |
| **Approval scope** | The workspace declared in `docs/ui/design-manifest.yaml`. Campaign Portfolio (portfolio and campaign detail), Retail health, and Prompt Administration carry the command-center visual language. From revision `2026-10-08.1`, the Scorecard, Reach and funnel, Audiences, Retention, and Repeat purchase by product packages, and from revision `2026-10-09.1` the New customers package, carry the information-architecture visual language; each is implemented only in its own build slice, once that slice's prerequisites and route approval are met. The channel, product, marketplace, evidence-health, and media-efficiency pages are retired from the frontend; their presentation APIs remain deployed for reuse |
| **Owns** | The page roster, capability-to-page coverage, shared visual-evidence, temporal-control, theme-preference, mockup policy, and review criteria |
| **Parent** | [Architecture](../architecture.md) · [Conversational Runtime](../capabilities/conversational-runtime/README.md) |

## 1. Purpose

Decision Workspace turns governed analytical artifacts into decision-oriented review flows. It does not create browser-side rankings, recommendations, calculations, causal claims, period derivations, or metric rollups. A user selects a server-declared surface, sees scope and qualification, then follows one generated permitted action.

## 2. Approved page roster

| Page | Intended route | Primary job | Artifact status | Package | Mockup |
|---|---|---|---|---|---|
| [Campaign Portfolio](campaign-portfolio/README.md) | `/campaigns/` — Performance hub | Triage the portfolio, then command-center one campaign in place: tiles, trends, insight slots, [hardened metric focus](metric-focus-contract.md). | Command-center mandate: `CampaignPortfolioArtifact.v1` and CampaignAttribution V2 required. | [Design](campaign-portfolio/high-level-design.md) · [Technical](campaign-portfolio/low-level-design.md) | [Open](campaign-portfolio/mockup.html) |
| [Retail health](retail-health/README.md) | `/retail-health/` | Triage 1P retail movement: shipped/ordered outcomes, returns, traffic, reconciliation, insight slots. | `RetailHealthArtifact.v1` required; served from publish-time artifacts ([ADR-070](../decisions/adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md)). | [Design](retail-health/high-level-design.md) · [Technical](retail-health/low-level-design.md) | [Open](retail-health/mockup.html) |
| [Prompt Administration](prompt-administration/README.md) | `/admin/prompts/` — operators only | Version, calibrate, activate, and retire insight-slot prompts. | Registry API, store, and calibration runner required ([ADR-069](../decisions/adr-069-version-insight-prompts-in-a-calibration-gated-registry.md)). | [Design](prompt-administration/high-level-design.md) · [Technical](prompt-administration/low-level-design.md) | [Open](prompt-administration/mockup.html) |
| [Overview › Growth check](overview/README.md) | `/` — Overview hub | Whether advertising is growing the business: the once-counted return, ad spend, shipped retail sales, TACoS, daily spend and sales, new customers per channel, what changed and trust lines, all for 7 days. | A composed tab over the served Scorecard, Retail health, New customers and Campaigns presentations ([ADR-151](../decisions/adr-151-compose-a-customer-tab-from-served-page-presentations.md)); no artifact of its own. Built; on `main` since PR #8 and live on dev since 2026-10-22. | [Bands and sources](overview/README.md) | — |
| [Retail health › Products and ads](products-and-ads/README.md) | `/products-and-ads/` — Retail health hub | Each product's served shipped sales and page views beside what one chosen ad channel credits to it, from the product's baked detail. No product return on ad spend. | A composed tab over the served Retail health parent and its baked product details ([ADR-151](../decisions/adr-151-compose-a-customer-tab-from-served-page-presentations.md)); no artifact of its own. Built; on `main` since PR #12; captured on dev 2026-10-23. | [Columns and sources](products-and-ads/README.md) | — |
| [Scorecard](scorecard/README.md) | `/scorecard/` — Performance hub | Return on ad spend per network and channel: the deduplicated network headline, the ranking under each attribution model, the channel table, TACoS, and spend across networks. | Designed at `2026-10-08.1`; `NetworkScorecardArtifact` v1, its materializer, route, and `network-scorecard` insight contract required. Built; live-accepted on dev 2026-10-18 ([D2 live acceptance](../../validation/RESULTS.md#d2-pages-and-graph-control-plane-live-acceptance-on-dev--2026-10-18)). | [Design](scorecard/high-level-design.md) · [Technical](scorecard/low-level-design.md) | [Open](scorecard/mockup.html) |
| [New customers](new-customers/README.md) | `/new-customers/` — Customers hub | New-to-brand purchases per channel, new buyers and cost per new buyer for complete weeks and months, for the network and per channel, and gateway products one channel at a time. | Designed at `2026-10-09.1`, revised at `2026-10-09.2` (per-channel new buyers); `NewCustomersArtifact` v2, its materializer, route and `portfolio-acquisition` insight contract, the platform and per-channel new-to-brand buyers packs and `amc-attributed-conversion` pack version 2 required. Built; live-accepted on dev 2026-10-18 ([D2 live acceptance](../../validation/RESULTS.md#d2-pages-and-graph-control-plane-live-acceptance-on-dev--2026-10-18)). | [Design](new-customers/high-level-design.md) · [Technical](new-customers/low-level-design.md) | [Open](new-customers/mockup.html) |
| [Reach and funnel](reach-and-funnel/README.md) | `/reach/` — Reach and audiences hub | Deduplicated reach and overlap between DSP and sponsored ads, full funnel by exposure group, and frequency, for one complete period, labelled association, not cause. | Designed at `2026-10-08.1`, revised at `2026-10-09.2` (search rate label); `ReachAndFunnelArtifact` v1 and AMC packs 5 to 7 required. Built; week and month live-accepted on dev 2026-10-18, quarter not ([D2 live acceptance](../../validation/RESULTS.md#d2-pages-and-graph-control-plane-live-acceptance-on-dev--2026-10-18)). | [Design](reach-and-funnel/high-level-design.md) · [Technical](reach-and-funnel/low-level-design.md) | [Open](reach-and-funnel/mockup.html) |
| [Audiences](audiences/README.md) | `/audiences/` — Reach and audiences hub | Which audience segments buyers belong to compared with reached shoppers, and DSP campaigns by segment, for the latest complete month. | Designed at `2026-10-08.1`, revised at `2026-10-09.2` (provider labels; separate segment IDs); `AudienceProfileArtifact` v1 and AMC packs 8 to 13 required. Built; live-accepted on dev 2026-10-18 ([D2 live acceptance](../../validation/RESULTS.md#d2-pages-and-graph-control-plane-live-acceptance-on-dev--2026-10-18)). | [Design](audiences/high-level-design.md) · [Technical](audiences/low-level-design.md) | [Open](audiences/mockup.html) |
| [Retention](retention/README.md) | `/retention/` — Customers hub | Cohorts of new customers the ads brought in and when they buy again, per vintage. | Designed at `2026-10-08.1`, revised at `2026-10-09.2` (calendar-quarter cohort state); `RetentionCohortArtifact` v1 and AMC packs 14 and 15 required. Built; live-accepted on dev 2026-10-18 ([D2 live acceptance](../../validation/RESULTS.md#d2-pages-and-graph-control-plane-live-acceptance-on-dev--2026-10-18)). | [Design](retention/high-level-design.md) · [Technical](retention/low-level-design.md) | [Open](retention/mockup.html) |
| [Repeat purchase by product](repeat-purchase-by-product/README.md) | `/repeat-purchase/` — Customers hub | Orders, customers, and repeat revenue per product for the latest complete month, reported by Amazon Brand Analytics, levels only. | Designed at `2026-10-08.1`; `RepeatPurchaseByProductArtifact` v1 and the Brand Analytics contract required. Not implemented. | [Design](repeat-purchase-by-product/high-level-design.md) · [Technical](repeat-purchase-by-product/low-level-design.md) | [Open](repeat-purchase-by-product/mockup.html) |

The unsupported Review Queue is intentionally absent. Campaign Portfolio is the entry to campaign review; it owns the server-authorized list-to-detail transition rather than duplicating a workspace ranking or recommendation surface.

## 3. Capability coverage and prerequisites

| Source-backed capability | Decision Workspace treatment | Technical prerequisite before production |
|---|---|---|
| Marketplace ad-spend and shipped-retail movement | Not rendered. The marketplace-comparison presentation API remains deployed for a future command-center surface. | A new page declaration in the command-center language before any route, decoder, or navigation entry exists. |
| Governed campaign catalog | Campaign Portfolio selection list over the full authorized population: baked ordered segments per declared sortable column, pagination state with the boundless total, and a baked name roster for estate filtering ([ADR-071](../decisions/adr-071-reach-the-full-campaign-estate-through-baked-ordered-segments.md); engine-side grouped measurement per the campaign LLD). | Segment and roster bake, the `segment` request parameter, pagination controls, and the roster filter — design mandated 2026-09-17, implemented and live-verified 2026-09-24. |
| Campaign spend, default attributed ROAS, purchases, and purchase series | Campaign Detail initial cohort. | Existing `CampaignAttribution` selected-detail artifact; a metric-bearing portfolio requires a separate artifact/schema extension. |
| Delivery, acquisition, halo, and new-to-brand measures | Campaign Detail extension. | Add fixed server-computed slots, named evidence, source/grain/window qualifiers, and bounded comparison policy. |
| Spend, impressions, clicks, CTR, CPC, CPM, and DSP media quality | Campaign Detail delivery slots at campaign grain, and Campaigns' "Delivery per channel" under All channels and Sponsored ads, each ratio from that channel's own operands ([ADR-127](../decisions/adr-127-serve-the-campaigns-channel-filter-from-render-only-siblings-of-one-bake.md)). | A scope-level media page needs its own declaration first. |
| Reach, frequency, cost per reach | [Reach and funnel](reach-and-funnel/README.md): deduplicated reach, overlap and frequency per complete period. | Built; quarter not live-accepted ([README Known limits](../../README.md#known-limits)). No browser aggregation. |
| Ordered/shipped, returns, retail conversion, glance views, reconciliation | [Retail health](retail-health/README.md) — mandated 2026-09-15. | `RetailHealthArtifact.v2` with declared conversion/reconciliation and derived returns-and-margin metrics, distributor-view and selling-program typed absences, publish-time artifact serving, the shared metric-focus dialog, and product detail as a baked child served by `retail-health-product` ([ADR-125](../decisions/adr-125-serve-retail-product-detail-as-a-baked-child-of-retail-health.md)), which retires the request-time retail-product API. |
| Inventory trend | Deferred. | Confirm a governed inventory source, its time basis, and an artifact/API with typed availability before any visual/control exists. |
| Blended ROAS and TACoS | TACoS, labelled mixed basis, on the [Scorecard](scorecard/README.md), Growth check and Retail health; attributed returns are never blended across channels or networks. | Built. |
| Forecasting, saturation, incrementality | Deferred. | Explicit future initiation, accepted artifact/quality contract, implementation, and acceptance. |

## 4. Visual evidence and temporal controls

A visual is used when the returned evidence has a meaningful shape, comparison, or change over time that is clearer than a sentence or a table. It is evidence, not decoration and not a causal claim.

Every rendered visual derives from a presentation artifact that declares:

- its metric, unit, entity/cohort, source/basis, qualification, and availability;
- the server-resolved current period, valid comparison period when any, and display grain;
- bounded series or parallel values that are valid to show together; and
- the available temporal selections and any generated next action.

The UI may display server-declared windows such as 1 day, 7 days, 14 days, 30 days, and — only where a page declares it — bounded Custom, but it never assumes those choices exist for every page or metric. A selection request is validated and resolved on the server. Custom dates are bounded and may return a different display grain, no valid comparison, or a typed unavailable state. The browser does not derive prior periods, aggregate points, calculate deltas, or blend series.

### 4.1 Hybrid server-declared temporal controls

The [Hybrid temporal-control contract](temporal-control-contract.md) is the shared implementation baseline. A page returns either a fixed resolved window or an ordered server-declared hybrid control with candidate `1D`, `7D`, `14D`, `30D`, and optionally bounded Custom choices. Each page may declare a runtime subset — Campaign Portfolio declares presets only ([ADR-070](../decisions/adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md)) — and the returned availability, exact bounds, resolved window, comparison status, display grain, reason, and recovery are authoritative.

The browser submits only the declared preset or Custom intent. A changed window creates a new parent/list generation and invalidates its dependent selection capabilities, child/detail evidence, and narration. `window_unavailable` is a control result distinct from an artifact’s analytical evidence state. Each page LLD owns its route-specific choices, comparison policy, artifact binding, and closed reason vocabulary.

A chart always has a title, scope/basis label, caption, visible qualification, and exact-data alternative. When a source cannot supply a valid series—such as an inventory history not yet backed by an artifact—the page renders an explicit unavailable/next-prerequisite state instead of an empty or invented graph.

### 4.2 Visual narration

An eligible visual may carry a compact, server-owned narration companion. It is anchored to one returned visual and its exact artifact revision; it refreshes whenever the resolved entity, period, window, grain, or qualification changes. It contains only these distinct blocks:

- **Observed** — an evidence-bound description of the returned shape, comparison, or state.
- **Perspective** — an optional agent judgment about why the observation may matter to a decision-maker. It is visibly distinct from Observed and cannot claim causation.
- **Hypothesis** — an optional, explicitly unproven explanation plus a safe validation direction.
- **Limit** — the required replacement when evidence, qualification, or narrator availability cannot support a point of view.

The server builds narration from the same resolved visual artifact; the browser does not send chart data, page context, or an inferred prompt to create it. The companion exposes the visual's scope, period, basis, grain, and material qualification. It is not silently added to the global assistant conversation, and the chart remains complete and usable when narration is unavailable.

| Narration delivery status | Required presentation |
|---|---|
| No eligible visual | Render deterministic artifact evidence only; no narration region is implied. |
| Planned or blocked | Render deterministic Observed and Limit content only, visibly labelled as planned or unavailable. Do not render Perspective or Hypothesis as delivered behavior. |
| Enabled | Render a server-minted revision-bound narration reference only after the shared [Visual Narration Harness](visual-narration-harness.md) custody, cancellation, fallback, and non-production acceptance gates pass. Perspective and Hypothesis remain optional. |

A page LLD owns each eligible visual ID and its reference binding. A mockup may illustrate an enabled target only when its narration region visibly states the relevant delivery status.

## 5. Shared page composition

Every page renders in this order:

1. **Workspace scope** — reporting period, selected subject, available window control, and material qualification.
2. **Decision summary** — one typed conclusion, state, or explicit limitation.
3. **Key outcomes** — a compact set of server-returned values and valid deltas.
4. **Visual evidence** — one primary trend or comparison when the artifact supports it, accessible exact values, and a graph-scoped Observed/Perspective/Hypothesis/Limit companion when eligible.
5. **Evidence detail** — semantic table, coverage, freshness, suppression, source, and boundary disclosure.
6. **Next action** — generated permitted controls only; no text that merely sounds actionable.
7. **Assistant** — persistent workspace sidebar, overlay, or sheet; it does not replace deterministic page evidence.

The shell uses navigation, primary evidence, and the assistant inspector. Cards carry interactive selections, outcome states, compact evidence, and assistant evidence—not routine grouping that headings and dividers express more clearly.

**One row per card group (mandated 2026-09-23).** A group of cards renders as a
single full-width row of equal columns, however many cards it holds: no holes from short
groups, no orphan wrapping from long ones, and every card in the row stretched to the same
height. Wrapping is a viewport concern only (compact layouts stack), never a count
concern. Cards with nondeterministic body height (insight cards) additionally follow the
placement and uniform-geometry rules the
[insight-presentation contract](insight-presentation-contract.md) owns.

## 6. Assistant response policy

The [workspace assistant](workspace-assistant.md) always remains available and uses the global server-governed tool surface. It asks a clarification only when metric, identity, period, grain, or required selector cannot be safely inferred. For data-backed responses it preserves natural-language prose, states transparency, then may place bounded server-derived evidence or visual cards inline in the same streaming assistant turn.

Perspective and Hypothesis remain visibly distinct from observed facts. The assistant's prose never changes a metric, extends scope, hides a material qualification, or turns a hypothesis into an observed fact.

## 7. Durable mockup policy

The [mockup gallery](mockups/index.html) and each linked page mockup are versioned design references. They use fictional labels and values, have no scripts, API calls, authentication, or production imports, and are not acceptance evidence.

A child page owns the mockup that illustrates hierarchy, state presentation, permitted controls, visual-evidence intent, and responsive behavior. When review changes a page, update its specification and mockup in the same change. Every page package carries one human-readable **Design revision** in its README, the matching `meta[name="design-revision"]` in its mockup, and the same value in the gallery. That revision advances only when the package README/HLD/LLD and mockup describe the same approved behavior. [`design-manifest.yaml`](design-manifest.yaml) declares each page/shared-owner closure, and its generated projection records every member digest; `make check-design-manifest` rejects stale closure or revision alignment. Production code may not copy mockup markup or styling directly; it reimplements approved behavior through typed React components and governed contracts.

## 8. Shared implementation guardrails

- The browser never computes analytical values, ranks, deltas, period relations, comparison results, or visual aggregates.
- Monetary display (owner mandate 2026-10-01, [ADR-084](../decisions/adr-084-display-monetary-values-at-currency-scale-from-a-server-display-value.md)): a currency value displays the server's `display_value`, two decimal places with ties rounded away from zero. Exact-values disclosures and reconciliation discrepancies show the exact value. The browser groups digits and appends the ISO code; it never rounds.
- Ratio display (UI refresh 2026-10-22, plan D4): the page-binding generator projects each dictionary metric's `units` and `ratio_scale` into `frontend/generated/page-bindings.json` (`metric_display`). A rate reads as a two-place percentage (`7.83%`), a multiplier as a two-place `×` (`2.24×`) and a count digit-grouped (`438,444`), rounded half up for display only by `displayFigure` in `frontend/lib/exact-format.ts`; the exact value stays in the "Show exact values" disclosure. Served display text (Reach, Audiences) is regrouped, never re-derived. Currency and count charts start at zero (ADR-118 decision 7).
- Answer sentence (UI refresh 2026-10-22, slice 1.5): every customer page leads with one sentence (`frontend/components/AnswerSentence.tsx`) built by a pure function in that page's `frontend/lib/<page>.ts` from served display strings and the served change `direction` only. It fills a fixed template, or its fallback when a slot is absent, and never computes, sums or ranks; each builder's doc comment names its template and fallback.
- Opaque entity selections never appear in visual labels, URLs, browser storage, or mockup data.
- Empty, covered-empty, suppressed, insufficient, unavailable, page-issue, expired-selection, clarification-required, limited, and refused states remain distinct.
- The [workspace assistant](workspace-assistant.md) uses global server-governed tools; it receives no browser page artifact or page authority.
- Assistant response actions resolve only to a visible generated approved control; unknown actions fail closed.
- Recovery actions are executable server-authorized controls or explicit no-self-service outcomes.
- Mockups and production surfaces support light/dark themes, keyboard navigation, visible focus, wide/regular/compact layouts, and accessible visual-data alternatives.

### 8.1 Theme preference and restrained glass

The [theme preference and restrained-glass contract](theme-preference-contract.md) owns System/Light/Dark selection, local presentation-only persistence, root token resolution, keyboard behavior, glass fallbacks, and shell acceptance. The theme trigger is a shared app-shell control: it must not change a route, artifact, capability, reporting window, page request, assistant conversation, or analytical value. Shared chrome and assistant/inline evidence may use high-contrast restrained glass; deterministic evidence surfaces remain opaque.

## 9. Review criteria

Approve a page only when its mockup and specification answer all of these:

1. Can a user identify the job, scope, and material qualification by scanning?
2. Is the decision summary clearer than raw evidence alone?
3. Does each visual add a valid comparison or trend rather than decoration, and does any narration remain bound to its exact returned scope?
4. Does every temporal control reflect server-declared availability and resolved scope?
5. Does each action preserve artifact scope and lead somewhere real?
6. Does the page remain useful without the assistant?
7. Can wide, regular, and compact layouts preserve the same evidence and keyboard access?
8. Does the shared theme trigger support System, Light, and Dark without affecting analytical state, while preserving opaque evidence and accessible glass fallbacks?

Implementation of a manifest revision requires the parent **Implementation disposition** `approved_for_implementation` and a dated **Implementation decision date**, scoped by the **Approval scope** row; `make require-approved-design-manifest` refuses otherwise.
