# Information architecture

| | |
|---|---|
| **Level** | Proposed design for the whole app's organisation and rendering |
| **Status** | Proposed. The owner reviewed and approved the mockups on 2026-10-07. The workflow orchestrator approved the design the same day under the owner's delegated overnight authorization, with the owner's open decisions taken as recommended except where an ADR names the owner's own call. Not implemented, and no implementation is authorized by this document alone: each build slice in [Delivery slices](delivery-slices.md) carries its own approvals. When accepted, each page package gains its own design docs and joins the [design manifest](../design-manifest.yaml). The Scorecard, Reach and funnel, Audiences, Retention and Repeat purchase by product packages joined it at revision `2026-10-08.1` |
| **Owns** | The proposed hub and tab roster, where each capability lives, what happens to every existing page and API, the render-more re-audit, and the change list for every existing page |
| **Parent** | [Decision Workspace UI](../README.md) |

The reasons for each choice are in the ADRs listed in [section 9](#9-decisions). This document says
what the proposed app is. Mockup figures and every example here are fictional.

## 1. Purpose and scope

Adlytics AI becomes a small set of hubs, each answering one customer question, plus one operator-only
group. Each hub has tabs, and each tab is one page with one window. A thin Overview at `/` shows a few
headline figures for one window and points into the hubs. This document covers navigation, page
boundaries, placement and page changes. Server contracts live in the ADRs, and the build order in
[Delivery slices](delivery-slices.md).

Out of scope: phone width, which the owner ruled out on 2026-10-05, and anything that writes to
Amazon. The platform is read-only.

## 2. Hubs and tabs

Hubs and tabs are listed in navigation order. A hub with one visible tab has no tab strip.

| Hub or group | Customer question | Tab and route | Tab description |
|---|---|---|---|
| Overview | "What needs my attention this week?" | Overview, `/` | What needs your attention this week, with a few headline figures for one window. |
| Performance | "Is my ad spend paying back, by network, channel and campaign?" | Scorecard, `/scorecard/` | Return on ad spend per network and channel |
| | | Campaigns, `/campaigns/` | Every campaign, for all channels, sponsored ads only or one channel |
| Reach and audiences | "Who did my ads reach, how often, and which audiences responded?" | Reach and funnel, `/reach/` | How many shoppers your ads reached, how often, and what they did next |
| | | Audiences, `/audiences/` | Which Amazon audience segments your buyers belong to, compared with the shoppers your ads reached, and how DSP campaigns did per segment |
| Customers | "Am I winning new customers, and do they come back?" | New customers, `/new-customers/` | Shoppers buying from your brand for the first time in 12 months |
| | | Retention, `/retention/` | Cohorts of new customers your ads brought in, and when they buy again |
| | | Repeat purchase by product, `/repeat-purchase/` | Orders, customers and repeat revenue per product, reported by Amazon Brand Analytics |
| Retail health | "How is my retail business doing on Amazon?" | Retail health, `/retail-health/` | Sales, traffic, conversion and returns from Vendor Central, per product |
| Plan (listed when the simulator ships) | "What might happen if I moved budget?" | Budget simulator, `/simulator/` | Try a budget move and see its modelled effect. Nothing changes in Amazon. |
| Operations group (operators only) | "Is the data current, and are the prompts right?" | Prompt administration, `/admin/prompts/` | Version, check and activate the prompts that write insights |
| | | Data pipeline status, `/admin/status/` | Whether each data feed is landing, and which pages are current |

Global, on every page: a top bar with the Assistant toggle, Theme and Sign out; the assistant docked
and open by default; and a real 404 page. The Alerts badge and inbox are designed but deferred,
because the owner deferred the alert evaluator on 2026-10-07. Each tab with deterministic findings
shows an "Also changed" section instead.

Data pipeline status is listed only where it's provisioned
([ADR-115](../../decisions/adr-115-organise-the-app-into-customer-question-hubs-from-one-navigation-file.md)
decision 8).

## 3. Site map

**Site map diagram — Adlytics AI app, proposed target state, navigation viewpoint, hubs to tabs and in-place children.**
Each hub's nav item lists its tabs; campaign detail and product detail open in place on their parent's
route.

![Site map diagram: Adlytics AI app, proposed target state, navigation viewpoint, hubs to tabs and in-place children. Six customer hubs and one operator group each list their tabs. Campaigns opens campaign detail in place, Retail health opens product detail in place, and a Scorecard channel row opens Campaigns. Dashed elements aren't built yet; solid elements exist today and are reworked. The Operations group sits inside an operator trust zone that the API entitlements enforce.](diagrams/site-map.svg)

> **Takeaway.** Six customer hubs and one operator group each list their tabs, and the two detail
> views open in place on their parent tab's route.

Other in-content links: Overview figure cards open the Scorecard, New customers and Retail health, and
a pointer card opens Reach and funnel. The Scorecard points to New customers, New customers to
Audiences, and Retention and Repeat purchase by product to each other. Retail health and product detail
point to Repeat purchase by product. Each such link names the target's own dates.

**Navigation by width.**

| Width | Navigation | Assistant | Main column |
|---|---|---|---|
| 1420 px and wider (designed at 1440) | 232 px sidebar | Docked, 360 px default | Capped at 832 px |
| 881 to 1419 px (designed at 1280) | 88 px labelled rail; the full label is the accessible name and a tooltip that stays while hovered or focused, until Escape | Docked, clamped so the main column keeps 448 px | 832 px at 1280 |
| Below 881 px (designed at 768) | Menu button opens a slide-out sheet with Theme and Sign out | Docked, fixed 320 px | About 448 px at 768 |

## 4. Keep, rework or retire

Each item is judged on merit
([ADR-117](../../decisions/adr-117-keep-pages-and-apis-only-on-merit-and-retire-a-replaced-surface-with-its-replacement.md)).
Everything kept is rebuilt in the new design. Nothing is merged.

**Pages and frontend surfaces.**

| Item | Evidence | Decision |
|---|---|---|
| Campaign portfolio list | `frontend/components/CampaignPortfolioV2View.tsx:989`; `presentation/pages/v2/pages.yaml:5-56` | Rework as Performance › Campaigns, with the channel filter |
| Campaign detail | `CampaignAttributionDetail` v2, `pages.yaml:28-31` | Rework; gains a Reach slot on the 1-day preset |
| Evidence status sheet | `frontend/components/EvidenceStatusSheet.tsx:64-112` | Rework as "Coverage for this window" on every tab |
| Insight and metric focus dialogs | `frontend/components/InsightFocusDialog.tsx:52`; `MetricFocusDialog.tsx:170` | Rework on one dialog primitive |
| Retail health | `frontend/components/RetailHealthView.tsx:392`; `pages.yaml:272-307` | Rework; rows open product detail |
| Prompt administration | `frontend/components/PromptAdminPage.tsx:151` | Rework to the owner's 2026-10-05 layout: readable slot names, and a roster that leads with the active version |
| Assistant panel | `frontend/components/ChatPanel.tsx:170`, launcher `:608-618` | Rework: docked, no launcher, page context |
| App shell and auth states | `frontend/components/AppShell.tsx:74`, hand-written operator link `:211-220` | Rework from the route manifest; auth states lightly |
| Channel Robustness page | `pages.yaml:57-101`, never rendered | Retire; its reading renders on the Scorecard |
| Media Efficiency page | `pages.yaml:219-270`, never rendered | Retire; its delivery facts render in Campaigns and campaign detail |
| Product Evidence page | `pages.yaml:103-147`, never rendered | Replace with product detail, then retire in that slice |
| Marketplace Movement page | `pages.yaml:149-181` | Retire; the Scorecard and TACoS row cover it |
| Evidence Health page | `pages.yaml:183-217` | Retire; Reach and funnel and Data pipeline status cover it |
| v1 decision overview | `presentation/pages/v1/pages.yaml:14-47` | Retire; replaced by the Overview |

**APIs.** Paths are in `src/adapters/inbound/presentation_handlers/handler.py:486-499` and Amazon API
Gateway routes in `infra/lib/stacks/api-stack.ts:1258-1271`.

| API | Decision |
|---|---|
| `campaigns` | Keep, extend: `filter_id` and a `30d` preset ([ADR-127](../../decisions/adr-127-serve-the-campaigns-channel-filter-from-render-only-siblings-of-one-bake.md)) |
| `campaign-detail`, `campaign-metric-focus`, `portfolio-metric-focus` | Keep, extend: change semantics, next steps, page context, detail v3 with `reach`, `filter_id` on portfolio focus |
| `retail-health`, `retail-metric-focus` | Keep, extend: the same, plus a product selection capability on focus ([ADR-125](../../decisions/adr-125-serve-retail-product-detail-as-a-baked-child-of-retail-health.md)) |
| `insight-deep-dive` | Keep, extend: `filter_id` |
| `overview` | Keep the path and route, replace the request-time handler with the baked Overview |
| `amazon-channel-performance` | Retire; its reading is baked into the Scorecard ([ADR-126](../../decisions/adr-126-render-the-scorecard-ranking-under-each-attribution-model.md)) |
| `media-efficiency/scopes`, `/artifact` | Retire. No API Gateway route reaches it |
| `retail-product` | Replace with `retail-health-product`, then retire in that slice ([ADR-125](../../decisions/adr-125-serve-retail-product-detail-as-a-baked-child-of-retail-health.md)) |
| `marketplace-comparison`, `data-health` | Retire |
| Prompt registry, `api-stack.ts:1200-1231` | Keep |
| Operational status, `api-stack.ts:1238-1250` | Keep; rendered as Data pipeline status |
| Conversation, `src/adapters/inbound/conversation/turns.py` | Keep; `ConversationRequest.v3` replaces v2 ([ADR-120](../../decisions/adr-120-carry-page-context-to-chat-as-a-server-redeemed-reference.md)) |
| Auth and `/me`, `src/adapters/inbound/auth_handlers/handlers.py:358-361` | Keep, extend with `can_read_operational_status` and `operational_status_provisioned` |
| Quality review `turn`, `turns` | Keep, no UI |

## 5. Render-more re-audit

Applied under [ADR-124](../../decisions/adr-124-render-governed-data-and-retire-a-surface-only-on-evidence.md).

| Surface | Decision | Home |
|---|---|---|
| Product drill-in | Render | Retail health › product detail |
| Channel delivery: clicks, CTR, CPC, CPM | Render | Campaigns › By channel, "Delivery per channel" table under All channels and Sponsored ads |
| Amazon DSP viewable rate per channel | Render | Same table, only when the option includes Amazon DSP; sponsored rows read "—" with a reason |
| Ranking under each attribution model | Render | Scorecard network card, "Does the ranking hold under every model?" |
| Cost of goods shipped | Render, as dev renders it today | Retail health and product detail, "Traffic value and margin", beside the Amazon margin proxy |
| Average selling price, revenue per glance view | Render | Retail health and product detail |
| New-to-brand purchases per product | Point to the home | Customers › New customers › Gateway products |
| Amazon Marketing Cloud (AMC) retail purchases | "Available with the paid feature" | Retail health and product detail |
| Sponsored viewability, return reasons | Typed absence | Where a column or tile declares them |
| Media Efficiency audience slots | Retire | No data: hard-coded unavailable [repo: `src/domain/presentation/media_efficiency.py:241-243`] |
| Marketplace Movement, Evidence Health, v1 overview | Retire | Duplicates of facts with a home |
| Scorecard reach band, channel-group subtotal rows | Keep removed | A duplicate home, and a cross-channel attributed ratio |
| Scorecard quarter preset, Forecast tab | Keep deferred | No new data, or not approved yet |

## 6. Shared patterns

The patterns, tokens and conformance rule are decided in
[ADR-118](../../decisions/adr-118-render-every-page-from-one-set-of-shared-patterns-and-extended-tokens.md).
The contracts behind them:

| Pattern | Contract |
|---|---|
| Change chip | Server `change` block ([ADR-121](../../decisions/adr-121-declare-change-direction-and-sentiment-on-the-server.md)) |
| Next-step chips | `NextStep.v2` ([ADR-122](../../decisions/adr-122-declare-next-step-targets-as-a-typed-union-checked-against-navigation.md)) |
| Assistant context chip and starter questions | Page-context reference and `starter_questions` ([ADR-120](../../decisions/adr-120-carry-page-context-to-chat-as-a-server-redeemed-reference.md)) |
| 404 page | Manifest allowlist ([ADR-123](../../decisions/adr-123-serve-a-real-404-through-a-manifest-generated-allowlist.md)) |
| Drill-link dates | Pointer `resolved_window` ([ADR-116](../../decisions/adr-116-give-every-tab-one-time-model-and-one-window.md)) |
| Coverage sheet | `EvidenceStatus.v2` ([ADR-119](../../decisions/adr-119-serve-window-coverage-as-one-server-written-evidence-status-block.md)) |

## 7. Changes to existing pages

**Shell.** Navigation from the route manifest with one inline SVG icon set; labelled rail and slide-out
sheet; floating launcher removed; the sidebar footer "Every value is server-declared…" removed;
landmark order, h1 focus and `document.title` on a route change; sentence-case names ("Campaigns",
"Retail health").

**Campaigns.**

1. Hub header and tab strip replace the "Portfolio overview" band; the scope band names the window.
2. Shared date control, with disabled presets' reasons in place, and a new `30d` preset.
3. A channel filter held in the URL: a segmented control at two or three options, a "Channel" select at
   four or more.
4. Under a multi-channel option: spend and impressions totals; purchases and ROAS as "Not added across
   channels" tiles with the reason "Choose a channel to see it."; a "By channel" table with a
   Scorecard pointer line; a "Delivery per channel" table; a spend-only trend.
5. Metric focus, deep dives and insights follow the filter.
6. Server change chips, two-decimal ratios, a basis label on every ROI, a "Details ›" cue.
7. Equal-height insight row; charts with zero baselines and a stronger dashed prior.
8. Shared table, with the channel as a second line and a legend for every "—" reason.
9. "Find a campaign" in the table toolbar; Enter opens that campaign's detail.
10. Detail opens in place with the campaign as h1, the channel as a pill and Back closing it. The deep
    section renders or shows a neutral callout, failures show at the top with focus, a Reach slot
    shows on the 1-day preset, and the channel step targets the Scorecard.
11. `/` stops rendering Campaigns and becomes the Overview. From UI refresh slice 2.1 (2026-10-22) `/`
    is the composed Overview › Growth check tab ([ADR-151](../../decisions/adr-151-compose-a-customer-tab-from-served-page-presentations.md),
    [Overview](../overview/README.md)) listed under `composed_routes` in `presentation/navigation.yaml`,
    and no `pages.yaml` entry carries `/`. Between OD-UX-3 and that slice, `/` opened the Scorecard.

**Retail health.** One-tab hub header; products table titled "Shipped revenue per product", with the
title first and ASIN second, a per-row Stale pill and stacked rows when narrow; server change chips;
data fixed before display, with no data-quality notes; four tile groups shared with product detail; the
corrected "Units per 1K glance views"; one spelling of fulfilment; a link to Repeat purchase by product;
each row opens product detail.

**Dialogs.** Shared modal primitive with a sticky header; "What stands out" layout; one "How this is
calculated" disclosure; plain absence headings; "Ask the assistant" fills the composer and sends
nothing.

**Coverage sheet.** Titled "Coverage for this window", names the missing dates, fixes "a ad spend",
drops "Every line below is written by the server.", and renders only `EvidenceStatus.v2`.

**Assistant.** Context chip and its states, wrapped to two lines with the full text as its accessible
name; docked at every width; resize kept, with width buttons; server starter questions; "Copy
reference" with a toast; dark primary button text `#070b13`.

**Prompt administration.** Readable slot names; the roster leads with the active version; secondary
Activate buttons, primary only in the confirmation; no overflow; a wrapped prompt viewer.

**State pages.** A page error keeps the hub header and offers Try again and "Go to Overview"; a real
404; skeletons that match the layout; the product name and a heading on sign-in.

## 8. Where business outcomes and the simulator live

| Outcome | Home |
|---|---|
| ROI scorecard, TACoS, cross-network spend | Performance › Scorecard |
| New-to-brand customers and gateway products | Customers › New customers (moved off the Scorecard) |
| Repeat-purchase cohorts | Customers › Retention |
| Brand Analytics repeat purchase per product | Customers › Repeat purchase by product, levels only |
| Audience segments | Reach and audiences › Audiences |
| Deduplicated reach and overlap, full-funnel contribution, frequency | Reach and audiences › Reach and funnel, with the association-not-cause caveat inside each visual |
| Reach per campaign-day | Campaign detail, Reach slot, 1-day preset |
| Alerts | Deferred: "Also changed" per tab and the Overview's "What changed" card |
| Budget simulator | Plan › Budget simulator, listed when it ships. Plan totals and modelled ROAS show per channel only, and the budget change is totalled across channels, as the owner chose on 2026-10-07. The Amazon DSP forecast curve sits beside the custom curve, labelled and outside totals |

(OD-UX-4, 2026-10-22: the Scorecard's channel cards show each channel's served new-to-brand share and
cost per first-time-buyer purchase beside its return, reversing the "new-to-brand moves to New
customers" amendment below for those two figures; New customers still owns them, and the cards read
its served rows. [Scorecard LLD](../scorecard/low-level-design.md#channel-cards-ui-refresh-23-readout-a3-od-ux-4).)
The business outcomes design's owner applies its amendments: new-to-brand moves to New customers, the
Scorecard reach band and channel-group subtotal rows go, Repeat purchase by product joins navigation,
the Reach slot moves to campaign detail, alerts gain `filter_id`, baked findings ship either way, and
a new requirement adds the Scorecard ranking. The forecasting design's owner absorbs the per-channel
plan totals and two curve-state inputs: how each curve state (fit, pooled, weak, held) is drawn around
current and observed spend, and wording that tells a curve state from a scenario gate.

## 9. Decisions

- [ADR-115](../../decisions/adr-115-organise-the-app-into-customer-question-hubs-from-one-navigation-file.md): customer-question hubs from one navigation file
- [ADR-116](../../decisions/adr-116-give-every-tab-one-time-model-and-one-window.md): one time model and one window per tab
- [ADR-117](../../decisions/adr-117-keep-pages-and-apis-only-on-merit-and-retire-a-replaced-surface-with-its-replacement.md): keep on merit, no dual path
- [ADR-118](../../decisions/adr-118-render-every-page-from-one-set-of-shared-patterns-and-extended-tokens.md): shared patterns and extended tokens
- [ADR-119](../../decisions/adr-119-serve-window-coverage-as-one-server-written-evidence-status-block.md): one server-written coverage block
- [ADR-120](../../decisions/adr-120-carry-page-context-to-chat-as-a-server-redeemed-reference.md): page context in chat and starter questions
- [ADR-121](../../decisions/adr-121-declare-change-direction-and-sentiment-on-the-server.md): server-declared change semantics
- [ADR-122](../../decisions/adr-122-declare-next-step-targets-as-a-typed-union-checked-against-navigation.md): server-declared next-step targets
- [ADR-123](../../decisions/adr-123-serve-a-real-404-through-a-manifest-generated-allowlist.md): a real 404 page
- [ADR-124](../../decisions/adr-124-render-governed-data-and-retire-a-surface-only-on-evidence.md): the render-more rule
- [ADR-125](../../decisions/adr-125-serve-retail-product-detail-as-a-baked-child-of-retail-health.md): retail product detail
- [ADR-126](../../decisions/adr-126-render-the-scorecard-ranking-under-each-attribution-model.md): Scorecard ranking under each model
- [ADR-127](../../decisions/adr-127-serve-the-campaigns-channel-filter-from-render-only-siblings-of-one-bake.md): Campaigns channel filter and delivery per channel

## 10. Open items

| Item | What resolves it | Blocks |
|---|---|---|
| The Scorecard model selector changes no figure today | The owner of that business outcomes requirement keeps or retires it through an amendment | Nothing; it stays until then |
| The 404 status code | The 404 slice's dev probe records it; the guide was read in that slice ([ADR-123](../../decisions/adr-123-serve-a-real-404-through-a-manifest-generated-allowlist.md) decision 3) | Nothing; 200 is the recorded fallback |
| Lifting the dev-only guard on operational status | A separate infrastructure decision with its own cost and retention review | Data pipeline status outside dev |
| The alert evaluator | The owner adds it later as a control-plane workload | The Alerts badge and inbox |
| Simulator curve states and their wording | The forecasting design's owner | The Plan hub |
