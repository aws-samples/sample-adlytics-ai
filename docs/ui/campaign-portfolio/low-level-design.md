# Campaign Portfolio — low-level design

This document owns exact wire contracts, artifact schemas, capability custody, UI state, V2 extension seams, and acceptance. The [README](README.md) owns business scope; [high-level design](high-level-design.md) owns system topology and request-to-render lifecycle.

## 6. Wire, artifact, and renderer contracts

### Exact API inputs

| Route | Method/auth | Accepted body | Explicitly forbidden |
|---|---|---|---|
| `/api/presentation/campaigns` | `POST`, custom request authorizer | `{session_id}`, optionally `temporal_selection: TemporalSelection.v1` restricted to `omitted` or a declared preset, `sort: {field, direction}` where `field` is one of the artifact-declared sortable columns and `direction` is `asc` or `desc`, and `segment: n` — a bounded non-negative index into the baked ordering ([ADR-071](../../decisions/adr-071-reach-the-full-campaign-estate-through-baked-ordered-segments.md)); the default is the first segment of the artifact's declared server order. | owner, canonical campaign/channel/marketplace/platform, selection reference, page context, caller comparator, raw `visible_window`, custom temporal selection ([ADR-070](../../decisions/adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md)), undeclared sort fields, out-of-range segment indexes, free-text search input (filtering is client-side over the baked roster), unknown fields. |
| `/api/presentation/campaign-detail` | `POST`, same authorizer | `{session_id, selection_reference}` | temporal selection, visible/comparison window, owner, canonical selector, page context, unknown fields. |
| `/api/presentation/campaign-metric-focus` | `POST`, same authorizer | `{session_id, selection_reference, metric}` where `metric` is one of the insight contract's declared focusable slots; window, comparator, and grain derive from the redeemed capability's parent. | temporal selection, any window or grain field, owner, canonical selector, page context, unknown fields. |
| `/api/presentation/portfolio-metric-focus` (added 2026-09-17, [shared metric-focus contract](../metric-focus-contract.md)) | `POST`, same authorizer | `{session_id, metric, temporal_selection}` where `metric` is a declared focusable portfolio metric and the selection is restricted to `omitted` or a declared preset; the payload derives from the served baked portfolio artifact. | custom temporal selection, any window or grain field, selection reference, canonical selectors, unknown fields. |

The authorizer supplies owner and correlation identity. Session ID is nonblank/bounded; the reference is an opaque validated token. The list operation follows the shared [Hybrid temporal-control contract](../temporal-control-contract.md): it returns `PresentationResult.v2` with `TemporalControl.v1` and either a list artifact or a distinct `window_unavailable` result. The detail comparator is server-derived from the list snapshot; detail never accepts a replacement temporal selection.

Window resolution anchors to the newest published coverage of the page's sources, never to
wall-clock today: each preset's dates are derived backward from the coverage end, and an
unavailable preset states in plain words what coverage it needs. Campaign declares candidate
`1D`, `7D`, `14D` and no Custom entry: its temporal control returns
`custom.availability: "unavailable"`
([ADR-070](../../decisions/adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md)).
`30D` is not a candidate — the per-pack bake snapshot caps a declared preset at 25 days
(ADR-070 amendment, 2026-09-16) — so the control returns it unavailable pending the future
two-pass verified-snapshot decision named there. `1D` is a served candidate (declaration
exception removed 2026-09-17): the portfolio bakes and serves a one-day window with its
required one-day prior. A campaign opened at one day returns the declared `insufficient`
detail outcome with reason `too_short_series`, because V1/V2 purchase-series rendering
requires at least two points; a single-period, non-trend detail outcome remains the named
future change that would make one-day detail meaningful. The remaining
candidates are runtime-declared by the server. Campaign requires an equal-length prior
comparison: a selection without a valid compatible prior returns `window_unavailable`, rather
than a selected detail with a fabricated comparator.

An omitted selection resolves through one deterministic default ladder, evaluated at bake
time so the default is always a persisted variant: the preferred preset with its required
prior; else the next smaller available preset with prior; else the deterministic partial
window (the largest coverage-supported window below the preferred preset, halved by the
required prior); else `window_unavailable`. Every declared preset and the resolved default
must pass the build-time bake-ability check (per-pack snapshot bound and bake budget) before
it may be declared.

During the bounded negotiated migration, `presentation/pages/v1/pages.yaml` remains frozen for V1 clients and `presentation/pages/v2/pages.yaml` carries the V2 declaration. A V1 capability presented to a V2 request is a nondisclosing refresh-list condition. The V1 declaration and response branch are removed only after coordinated V2 contract, browser, and non-production acceptance.

A new resolved list window mints a new parent artifact and capability set. The returned parent’s `temporal.resolved.window` must match the list artifact period; every redeemed detail preserves that period, selection kind, display grain, and comparison semantics. Changing a window clears detail, capability, and narration state before the browser begins a new list generation.

### V1 public artifact

| Artifact section | V1 public fields | Rendering responsibility |
|---|---|---|
| List | choices, list evidence, period, outcome, reason codes, recovery actions, truncation. | Safe picker and typed list state. |
| Choice | safe signature plus `selection_reference` and `expires_at`. | Display signature and expiry; never persist canonical selection. |
| Detail summary | purchases, attributed ROAS, spend; exact units/basis/grain/period. | Outcome KPIs and prior comparison. |
| Detail series | dated purchases at date × campaign × marketplace. | Exact chart/table; no client total/change. |
| Evidence | canonical slot order, coverage, holes, suppression, units, basis, grain, freshness. | Evidence trace and insufficient state. |
| Boundary | fixed campaign interpretation statements. | Visible limitation disclosure. |

A V1 selected detail requires all summary evidence, a reconciled purchase series with at least two points, and complete prior comparison evidence. Any missing/suppressed/truncated/coverage/incompatible/one-sided condition produces canonical typed insufficiency.

**Component diagram — Campaign capability and artifact custody, data-lineage viewpoint.** A list capability is valid only with its immutable parent artifact revision, authenticated owner, conversation session, expiry, safe signature, and current catalog identity.

![Component diagram: Campaign capability and artifact custody. The presentation API mints a parent list artifact and one opaque campaign capability. A detail request validates that capability, parent artifact, owner, session, expiry, and current catalog identity before it constructs a child detail artifact.](diagrams/campaign-custody.svg)

> **Takeaway.** A campaign detail is authorized by a server-held capability and its exact parent artifact lineage, never by a browser-provided campaign identity.

### V2 delivery contract

V2 preserves every V1 meaning and adds fixed server-returned delivery evidence. It does not reinterpret default ROAS, derive ratios in the browser, or expose campaign-level deduplicated ROAS.

| V2 field group | Metrics | Grain | Availability rule |
|---|---|---|---|
| Delivery summary/comparison | impressions, clicks, CTR, CPC, CPM | period × campaign × marketplace | Required V2 cohort for eligible campaign detail. |
| Delivery series | ad spend, impressions, clicks, CTR, CPC, CPM | date × campaign × marketplace | Return exact server-planned daily points; CPC's daily points are derived from the accepted daily spend and clicks (see below). |
| DSP quality | measurable/viewable/estimated impressions, viewable rate | period/date × campaign × marketplace | DSP only. Sponsored is typed unavailable, never numeric zero. |
| Existing outcome | purchases, `roas_attributed_14d`, spend, purchase series | V1 grains | Preserve V1 semantics/reconciliation. |

The schema declares metric roles and V2 artifact version. The handler uses fixed current/prior summary and daily plans, asserts every metric/role/group, inspects evidence, and reduces typed conditions. CTR/CPM and viewable rate are planned server ratios. CPC is not planned (amended 2026-10-19): the server derives it per role from the accepted ad spend and clicks, rounded to the planner's four places, in both the request-time handler and the bake. Amazon DSP spend and clicks have split writers, and the DSP clicks writer carries no currency, so a planned CPC grouped by currency splits into half-rows with no value. Zero clicks has no CPC: the aggregate is `empty_covered` and the day is missing from the daily series. The frontend exact decoder validates field sets, slot order, units, basis, grain, comparison/reconciliation, and DSP/Sponsored distinctions before rendering.

`CampaignAttributionDetail.v2` is the closed selected-detail extension. It preserves every V1 field and adds exactly:

> **Amended 2026-10-01 ([ADR-084](../../decisions/adr-084-display-monetary-values-at-currency-scale-from-a-server-display-value.md), B4).** "It preserves every V1 field" stays true: V1 fields and
> meanings are unchanged. The V1 metric-value object itself gained a server-derived
> `display_value`, so the `CampaignAttribution` v1 detail artifact's bytes changed at an
> unchanged version, and V2 spreads it. Every currency metric value carries `display_value`
> (two places, ties away from zero); every non-currency value, including the DSP quality
> impressions and viewable rate, carries `null`. Artifact versions are unchanged because
> retained bodies evolve through render format 5 (ADR-070). The domain decoders accept a retained body in which no metric value carries `display_value`, and refuse a mixed body or a wrong value (the tolerant canonical rule), until every environment's CURRENT pointer is at render format 5 and one `PAGE_TTL` (1 h) has passed since the last ApiStack deploy.

```text
delivery: {
  outcome: "complete" | "qualified" | "insufficient",
  summary: [
    impressions, clicks, ctr, cpc, cpm
  ],
  comparison: {
    period: { start, end },
    slots: [impressions, clicks, ctr, cpc, cpm]
  },
  series: {
    visual_id: "campaign_delivery_daily",
    grain: "day",
    points: [{ source_date, ad_spend, impressions, clicks, ctr, cpc, cpm }],
    exact_rows: [{ source_date, ad_spend, impressions, clicks, ctr, cpc, cpm }]
  },
  dsp_quality: {
    outcome: "available" | "unavailable",
    measurable_impressions, viewable_impressions,
    viewable_impressions_estimated, viewable_rate,
    reason_code: string | null
  },
  evidence: [                       # ordered per-(slot, role) status; never empty (ADR-072)
    { slot, role: "current" | "prior" | "daily",
      outcome: "accepted" | { condition, counts?, explanation, recovery_reading } }
  ],
  recovery: one closed disposition or generated action,
  narration_reference: null
}
```

**Insufficiency is itemized
([ADR-072](../../decisions/adr-072-name-why-evidence-is-insufficient-with-governed-slot-conditions.md)).**
The acceptance predicate returns an accepted value or a typed refusal naming one condition
from the closed, domain-owned delivery condition vocabulary with safe integer context (for
example days present versus days expected) — never raw row values. The server serves each
condition's plain-language `explanation` and `recovery_reading` beside the code; the browser
renders the served words verbatim and maintains no code-to-copy table, preserving the page's
"plain words, never a reason code" rendering rule. An insufficient delivery or attribution
card is an opener: it presents a detail sheet (metric-focus dialog mechanics — inert page,
focus custody, Escape restores, compact full-height sheet) listing each metric's status with
its served words. Attribution's existing per-slot conditions gain the same served readings
and render through the same sheet. Every reduction to `insufficient` also emits one
structured lifecycle log event carrying the per-slot conditions.

> **Amended 2026-10-01 ([ADR-084](../../decisions/adr-084-display-monetary-values-at-currency-scale-from-a-server-display-value.md), B4).** Every metric value below also carries `display_value`:
> two places for currency, `null` otherwise. The exact `value` is unchanged and remains what
> the exact-values disclosures show; the tiles and the delivery evidence table show the
> display value. The domain decoders accept a retained body in which no metric value carries `display_value`, and refuse a mixed body or a wrong value (the tolerant canonical rule), until every environment's CURRENT pointer is at render format 5 and one `PAGE_TTL` (1 h) has passed since the last ApiStack deploy.

Every numeric slot is an exact server-returned decimal/string record with unit, basis, source, coverage, and qualification; an unavailable DSP-quality field is never numeric. `series.points` and `series.exact_rows` contain the same returned dates and values, ordered by source date. The V2 artifact may declare `visual_id` but keeps `narration_reference: null` until the shared Harness and a revision-bound page reference pass their gates. A V2 visual cannot render unless its `TemporalControl.v1.resolved` period/grain equals the detail period/grain and its exact rows are available.

### UI state and refresh contract

**State-transition diagram — Campaign Portfolio rendering lifecycle, UI-state viewpoint.** The controller discards stale responses, disables expired capabilities, refreshes choices after invalid redemption, and renders only strictly decoded typed artifacts.

![State-transition diagram: Campaign Portfolio UI lifecycle. The Campaign Portfolio UI transitions from list loading to a typed list outcome, selection loading, typed detail, or classified issue. Expired or invalid capabilities refresh the list, and stale responses are discarded.](diagrams/campaign-state.svg)

> **Takeaway.** The controller treats capability expiry and validation failure as a new-list event, preserving only safe display context while preventing stale detail rendering.

### Implementation seams and acceptance

1. Keep the frozen legacy `presentation/pages/v1/pages.yaml` and its generated V1 binding for negotiated V1 clients; add `presentation/pages/v2/pages.yaml` and its V2 projection for the six-page successor roster.
2. Extend domain slots, summary/series/quality records, exact serializer/decoder, and selected/insufficient reduction.
3. Extend handler batch planning, expected shapes, evidence assembly, and parent-linked artifact issuance.
4. Extend frontend request binding, strict V2 parser, controller version refresh behavior, and delivery/quality modules.
5. Deliver list issuance/detail redemption atomically at V2; V1 parent/capability after cutover triggers list refresh.
6. Add contract/domain/handler/frontend/browser/non-production tests for V2 data, source qualification, custody, expiry, stale responses, and unavailable states.

### Command-center contracts (mandated 2026-09-15)

> **Amended 2026-10-01 ([ADR-084](../../decisions/adr-084-display-monetary-values-at-currency-scale-from-a-server-display-value.md), B4).** Each row and total comparison's `current` and `prior`
> carry `display_value` (two places for currency, `null` otherwise) beside the exact value;
> `spend_spark` and the portfolio trend points do not. The artifact stays at version 1; render
> format 5 carries the display value and two-place movers text. The domain decoders accept a retained body in which no metric value carries `display_value`, and refuse a mixed body or a wrong value (the tolerant canonical rule), until every environment's CURRENT pointer is at render format 5 and one `PAGE_TTL` (1 h) has passed since the last ApiStack deploy.

**`CampaignPortfolioArtifact.v1` is required.** It extends the list response with server-owned
row metrics so the portfolio table and triage tiles render: per campaign — spend, default
attributed ROAS, purchases, CTR, each with an equal-length prior delta; a bounded spend spark
series at the resolved grain; freshness state. Plus portfolio totals (spend, purchases,
impressions, and a server-computed portfolio-grain ROAS) with prior deltas, a portfolio trend
series whose dated rows carry every chart-switchable metric (spend, portfolio ROAS, purchases)
so the metric switcher is a client column selection with no re-request, and server row order. Sort is a server request parameter re-issuing the list; the
browser never reorders. The campaign row count is bounded by the artifact declaration
(initially 50); rows beyond the bound are a typed truncation with the boundless total stated.

> **Amended 2026-10-05 ([ADR-087](../../decisions/adr-087-serve-a-ratio-over-a-zero-denominator-as-typed-absent.md)).** A row ratio
> (attributed ROAS, CTR) whose denominator is zero in its window has `value: null`, and that
> comparison has `absolute_change: null` and `percentage_change: null`. Additive metrics never
> carry a null. Retained bodies evolve through render format 6 at an unchanged artifact version;
> a format-5 body carries no null and decodes unchanged. Sorting by a ratio puts an undefined
> value after every number in both directions.

> **Amended 2026-10-07 ([ADR-121](../../decisions/adr-121-declare-change-direction-and-sentiment-on-the-server.md)).** Every
> portfolio, campaign detail summary and delivery comparison carries the server's `change`
> block (direction, sentiment from the metric dictionary's `favourable_direction`, unit,
> `display_text`, `not_meaningful`) in place of `percentage_change`. A comparison with an
> undefined side carries `change: null` beside `percentage_change: null`; a delivery
> comparison entry carries its change from that slot's current summary value. Retained bodies
> evolve through render format 7 at an unchanged artifact version: a format-6 body or sibling
> carries `percentage_change` and no `change` key, decodes unchanged, is served in that baked
> form (never with `change: null`), and its chip reads "Change unavailable" until its pointer
> moves. A body that mixes the two forms is refused. **Retired 2026-10-09:** the retained decode
> is gone from both sides, so a format-6 body or sibling is refused (owner revision of 2026-10-08:
> the API, a full re-bake and the browser deploy together). Dropping
> the percentage keeps the 50-row served form under the 256,000-byte cap (operational design).
> **Amended 2026-10-07 ([ADR-119](../../decisions/adr-119-serve-window-coverage-as-one-server-written-evidence-status-block.md)).** Render
> format 8 adds the window's `evidence_status` coverage block to a complete portfolio (`null` on
> an empty or insufficient one), and campaign detail serves its block at serve time from its
> per-slot statuses, naming the missing dates of a coverage hole or an incomplete daily series.
> A missing or partial day withholds a figure's change on tiles and rows
> ([insight presentation contract](../insight-presentation-contract.md) §2.4). A format-7 body
> carries no key and reads as uncovered. The block keeps the 50-row served form under the cap
> (operational design). **Retired 2026-10-09:** the format-6 retained decode and the format-7
> block-less read are gone from both sides. A body without the key is refused, and so is a
> complete body whose block is `null` (owner revision of 2026-10-08).
> **Amended 2026-10-07 ([ADR-122](../../decisions/adr-122-declare-next-step-targets-as-a-typed-union-checked-against-navigation.md)).** Render
> format 9 moves card next steps to `NextStep.v2`. The body bytes are format 8's; the new
> revision makes every cached insight slot re-fill. A served campaign step carries the
> capability minted for its row in the same response
> ([insight presentation contract](../insight-presentation-contract.md) §2.3).
> **Amended 2026-10-08 ([ADR-127](../../decisions/adr-127-serve-the-campaigns-channel-filter-from-render-only-siblings-of-one-bake.md)).** Render
> format 11 bakes every channel filter (All channels, Sponsored ads when two or more sponsored
> channels are present, and each channel present, in taxonomy order) as a render-only sibling of
> the preset's one batch. The pointer binds All channels; its custody's `filter_siblings` binds
> each other filter's body and custody by digest at `pages/…/{revision}/filters/{filter_id}.json`,
> and segments are keyed `{filter_id}/{ordering}/segment-NNNN`. Every body carries
> `channel_filter` (options and the selected id) and `channel_view`: `null` on a one-channel
> sibling, which keeps the four tiles and three trends (ROAS is typed absent on a day with no spend, and its line breaks there); on All channels and Sponsored ads, spend
> and impressions totals, two "Not added across channels" tiles for purchases and ROAS, "By
> channel" (spend and impressions per channel), "Delivery per channel" (clicks, CTR, CPC, CPM, and
> the Amazon DSP viewable rate when the option includes it, each ratio from that channel's summed
> operands at twelve places) and a spend-only trend. Each "—" in Delivery per channel has a served legend line: a ratio over a zero denominator
> ("no clicks", "no impressions") and a viewable rate a sponsored channel doesn't report.
> Delivery per channel reads coverage slots
> prefixed `channel_`; a viewable rate whose prior window misses days lists a `prior`-role entry
> right after its own, which withholds the change and names the days (ADR-118 decision 5). Rows
> no longer carry `spend_spark`, and the table has no Spend trend column: a 30-day spark on 50 rows
> put the served body over the cap, and a campaign's daily spend has its home in detail.
>
> Render since UI refresh (UX sweep fix 6, 2026-10-22; the served body is unchanged): the two served
> "Not added across channels" tiles render as one line, "Pick a channel to see its sales and
> return." (`CAMPAIGNS_PICK_CHANNEL`); "By channel" renders only "Delivery per channel", because the
> spend-and-impressions table repeated the Scorecard; the campaign table has no per-row Coverage
> column, and one line above it reads "Latest data {date}" or each distinct served staleness
> qualifier once with "See why" opening the coverage sheet; under a one-channel option the table
> adds "Sort by Attributed ROAS to find campaigns to fix; you make the change in Amazon Ads."
> (`CAMPAIGNS_SORT_HINT`). Tile priors name their served prior window ("Aug 8–14, 2026: …").
> `campaigns`, `portfolio-metric-focus` and `insight-deep-dive` take `filter_id`; a well-formed id
> the preset doesn't offer is refused with `ADLY-PRESENTATION-0005`, and the browser shows All
> channels for that code alone. The page-context chip names the option, All channels included.
> **Retired 2026-10-09:** the format-10 read (S8-M1) is gone from both sides. A body without
> `channel_filter` and `channel_view`, or with a row that carries `spend_spark`, is refused
> (owner revision of 2026-10-08: the integrated deploy rebakes every Campaigns pointer). The insight cache key gains the filter. Detail opens in place
> behind one history marker, and the Scorecard `open_page` step is served once the Scorecard is
> a routed tab. Deviation, recorded: when a window change closes detail because no single row
> matches, the browser writes "{campaign} isn't on this page of the list for {window dates}." from
> the served signature and window, where ia §7.2 item 11a step 3 asks for a server StateCallout
> naming the preset label; the event happens in the browser after the parent re-serves, so there
> is no response to carry the text.

**Population scale (decided 2026-09-15; estate reach mandated 2026-09-17,
[ADR-071](../../decisions/adr-071-reach-the-full-campaign-estate-through-baked-ordered-segments.md)).**
A cohort exceeding the row bound is ranked, never refused: the portfolio measurement is
grouped over the full authorized population, and portfolio totals, trend, and deterministic
insight findings (movers, contribution, concentration) always cover the full population, not
the displayed segment. The full population is reachable: at bake time the complete row set is
sorted once per declared sortable column and direction and sliced into **ordered segments** at
the row bound, all derived from the same measurement and carrying the same artifact revision.
The parent artifact embeds the first segment of the canonical sort; a `segment` request
parameter (with `sort`) serves any other page of any declared ordering through the normal
pointer-read path, minting selection capabilities onto the served rows. The former truncation
record is a **pagination record**: position, segment size, segment count, and the boundless
population total. Sort has one semantics — a sort request selects a baked complete ordering
and resets position to the first segment; the former two-regime split (serve-time reorder
below the bound, cohort selection above it) is retired. A baked **name roster** per (page,
variant, revision) — governed display labels and canonical-order positions only, no keys and
no measures — supports client-side estate filtering; choosing a match fetches the segment
containing that position. The roster is a display index, never an authority surface.
The **declared sortable columns** are the four row metrics (spend, attributed ROAS,
purchases, CTR) and the campaign display name, each in both directions; the canonical sort
is spend descending. The **displayed cohort** of a variant is the union of the first
segment of every declared ordering — deterministic at bake time, and the input to detail
baking below. Two wire rules: the parent response **embeds the roster** (compact
label-and-position entries; the ADR-071 review trigger owns the payload bound), so estate
filtering needs no second fetch; and every response keeps the same browser shape — selected
segment rows and pagination beside the parent totals, trend, evidence, insights, and full roster.
The browser never joins or filters population arithmetic.

**Immutable segment custody (implemented 2026-09-18).** One full-population render produces a
parent public body containing only canonical spend-desc segment zero plus population aggregates
and roster. Parent custody contains that segment's canonical selectors and a closed descriptor
index for every noncanonical segment (including index zero) and every canonical tail segment.
Each descriptor binds ordering, segment index, public digest, and custody digest under the parent
revision. Publication create-or-attests every sibling public/private pair before parent public
and custody bodies, then advances the existing pointer; any missing, stale, substituted, or
misaligned body leaves the pointer unchanged or makes serving fail closed. Canonical segment zero
reads no sibling. Every other request follows pointer → parent/custody index → one sibling pair,
re-attests revision, ordering, pagination, digests, signatures, and mints no more than the 50
served-row capabilities. Every public, custody, and served page body is independently bounded at
256,000 encoded bytes.

**Insight slots** appear on both artifacts as a fixed `insights` section: per declared slot —
slot id, output class, state (`filled` | `silent` | `unavailable`), text and evidence line when
filled, artifact revision and prompt version stamps. The initial roster: portfolio —
contribution, divergence, exposure, movers; detail — trend-shape, efficiency-drift, funnel,
next-check; focus modal — metric-focus (parameterized by metric). The movers slot is fully
deterministic — server-ranked absolute changes rendered directly, no model call and no cache
entry; its contract declares no prompt and the registry never lists it. Slot declarations (purpose,
inputs, silence condition, output schema) live in versioned insight contracts under
`contracts/insights/`; the frontend renders slots strictly from the artifact and never requests
generation itself.

**Detail summary metric grid** (mandated 2026-09-24, superseding the 2026-09-17
"narrative metric sections" grouping and, for this strip only, the 2026-09-23 single-row
card-group rule). The detail surface's eight summary metrics render as one uniform grid of
two rows × four columns — every tile identical in size, no labeled section groups, no size
tiers. Narrow viewports stack in twos, then ones (a viewport concern, never a count one).
The ADR-072 insufficiency card still replaces the five delivery tiles with one full-width
card in the same grid; with only the three outcome tiles remaining, the grid narrows to
three columns so the first row stays full and the card forms the second — a hole or orphan
wrap is never rendered. The portfolio's four tiles keep the single-row rule unchanged, as
does every retail section row. The mockup is the exact composition reference.

**Metric focus** (hardened 2026-09-17; composition, states, and behavior owned by the
[shared metric-focus contract](../metric-focus-contract.md)). Both of this page's surfaces are
focusable, and every metric tile opens the dialog — the portfolio tiles' former
seed-the-assistant action moves inside the dialog's next-step block:

- **Detail surface** keeps the capability-bound `/api/presentation/campaign-metric-focus`
  route; its result widens to `MetricFocus.v3` (amended 2026-10-01, [ADR-084](../../decisions/adr-084-display-monetary-values-at-currency-scale-from-a-server-display-value.md)) carrying the metric-dictionary definition
  line, server-computed facts (peak day, trough day, largest single-day move, window
  value), and the ratio-component drivers (declared numerator/denominator per metric in the
  insight contract), beside the existing series and `metric-focus` slot.
- **Portfolio surface** gains a sibling parent-scope route
  `/api/presentation/portfolio-metric-focus` accepting `{session_id, metric,
  temporal_selection}` restricted to `omitted` or a declared preset. The server reads the
  same baked portfolio artifact the page was served from and derives facts plus per-campaign
  contribution drivers (each contributor's exact change and share of the total move) from
  its rows — no live measurement (ADR-070). The prompted slot serves cache-or-generate per
  (metric, window, artifact revision) through the registry's active version.

Modal behavior: page inert behind a scrim, Escape/✕ closes returning focus to the
originating tile, ‹ › hops metrics without closing, compact layouts present a full-height
sheet.

### Preset artifact publisher (mandated 2026-09-15, ADR-070)

The bake pipeline, artifact store, and serve path are shared machinery owned by the
[Preset artifact publisher](../preset-artifact-publisher.md) concept. This page currently instantiates
it with the portfolio measurement batch, ordered segments, and name roster (ADR-071), plus the default
ladder and bake envelope in the [operational design](operational-design.md) §1. Baking the **displayed
cohort's** complete campaign details per variant remains the mandated ADR-070 target. When that target
ships and approaches its envelope, the named decomposition is per-campaign detail messages fanned out
after the portfolio bake publishes its revision.

**Detail has two declared target tiers (ADR-070 amendment, 2026-09-17).** The displayed cohort is to
serve from a baked detail artifact, while a tail campaign—reachable through paging or the roster
filter—retains the capability-bound live measurement path on open. In the current implementation,
displayed details also use that live concurrent path until their bake target ships. Both paths render
the identical artifact contract; the browser can observe only latency, not the custody mechanism. Tail
insight slots generate on first open into the revision-keyed cache (first-touch warming: the cost is paid
once per campaign per revision by the first opener). Named future extension, not in scope until its
trigger fires (observed first-open latency on tail campaigns): re-warm recently opened campaigns at
publish, driven by insight-cache telemetry.

**Latency budgets.** Owned by the [operational design](operational-design.md), together with
caching, authorization, data protection, failure containment, observability, and cost.

**Assistant seeding.** An "Ask" chip (inside the focus dialog's next-step block or the
page-level ask row — a metric tile's primary action is the focus dialog, never a direct
seed) fills the workspace assistant's input
with server-composed question text naming the scope in words (metric, campaign display name,
resolved window) — it never auto-sends, and it carries no capability, artifact, or page
authority into the conversation. The assistant answers through its own governed tools exactly
as if the user had typed the question.

**Window-derived wording.** No rendered label, table title, column set, or grain word may
hardcode a grain or period; all derive from `TemporalControl.v1.resolved`. Designed absence
states are contract-level: empty metric tiles state why and the earliest window that fills
them; partially covered windows chart covered days beside a dated gap region with trim/keep
actions; every unavailable preset states its requirement in plain words.

## 7. Concepts applied

- [Shared visual and temporal policy](../README.md#4-visual-evidence-and-temporal-controls)
- [Workspace assistant](../workspace-assistant.md)
- [Workspace cutover](../implementation-cutover.md)

## 8. Decisions

Campaign delivery is a required V2 module, and `CampaignPortfolioArtifact.v1` is a required
command-center module (mandate of 2026-09-15). Insight slots follow
[ADR-068](../../decisions/adr-068-fill-declared-insight-slots-with-direct-structured-model-calls.md);
their prompts follow
[ADR-069](../../decisions/adr-069-version-insight-prompts-in-a-calibration-gated-registry.md).
Deduplicated ROAS remains in Channel Robustness: its current fact is channel-grain, model-qualified, and halo-inclusive.

## 9. Delivery prerequisites and acceptance

| Area | Required work or proof |
|---|---|
| Metric/catalog proof | Verify every planned V2 metric/source path, especially estimated viewability, has governed producer, compatible basis/grain, and source qualification. |
| V2 cutover | Versioned declaration, generated binding, domain types, strict codecs, atomic list/detail parent-version issuance, and V1 refresh behavior. |
| Handler | Fixed plans, exact metric/role/group assertions, evidence inspection, canonical slot order, and typed coverage/suppression/truncation states. |
| Temporal controls | Server-returned allowed presets (Custom declared unavailable), resolved period/grain, and comparison validity before controls become active. |
| Preset artifacts | Baked artifact per resolvable variant with revision derived from exact publication identities; parent carries only canonical first segment plus aggregates/roster; parent custody closes over every required sibling digest pair; all siblings attest before parent/pointer; noncanonical serving reads exactly one pair and performs no reorder or measurement; missing artifact or sibling fails closed. |
| Portfolio metrics | Required `CampaignPortfolioArtifact.v1`: metric cohort/order, per-row deltas and spark series, portfolio totals and portfolio-grain ROAS, per-row evidence, strict decoder, and generated drill bindings. |
| Insight slots | Insight contracts under `contracts/insights/`, deterministic finding computation, structured generation with declared-output-class checks (figure faithfulness is calibration- and evaluation-judged, ADR-082), revision-keyed cache table, preset precompute job (mandated replacement: the post-pointer insight-fill child reading the [generation materialization](../generation-materialization.md#53-preset-insight-slots)), slot states in both artifacts. |
| Prompt registry | Registry API, store, calibration runner, and operator boundary per the [Prompt Administration package](../prompt-administration/README.md). |
| Concurrency | Bake-time batch execution parallelized under a configured bound per `.kiro/steering/backend-concurrent-execution.md`; serve-route latency budget declared and met. |
| Campaign deduplication | New campaign-grain producer/fact, explicit model selector, viability/suppression proof, semantic metric, and versioned artifact. |
| Visual acceptance | Readable wide/regular/compact layouts; exact-data alternatives; no card content below readable width; DSP/Sponsored quality distinction; narration visibly separate from facts. |
| Tests | Unit/domain codec invariants; handler exact plans/custody/errors; frontend decoder/controller; browser journeys; non-production acceptance. |
