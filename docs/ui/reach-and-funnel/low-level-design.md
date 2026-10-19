# Reach and funnel — low-level design

## 6. Wire, artifact, and renderer contracts

Marker key: [repo] is read in this worktree on 2026-10-08 with `file:line`; [proposal] is a choice made in
this package that the build slice may still change through review.

### Exact API inputs

| Route | Method and auth | Accepted body | Explicitly forbidden |
|---|---|---|---|
| `/api/presentation/reach` [proposal] | `POST`, the custom request authorizer | `{session_id}`, optionally `temporal_selection: TemporalSelection.v2` restricted to `omitted` or a declared calendar preset | rolling presets, custom ranges, raw periods, identity-space or group selectors, channel or campaign selectors, unknown fields |
| `/api/presentation/reach-metric-focus` [proposal] | `POST`, same authorizer | `{session_id, metric, temporal_selection}` with a declared focusable metric | any grain, bucket or window field, unknown fields |

### Page declarations

New page id `reach`, route `/reach/`, governed label "Reach and funnel", in the Reach and audiences hub with
tab order 1.

| File | Entry |
|---|---|
| `presentation/pages/v1/pages.yaml` | `mode: single_basis`, `grain.time: declared_period`, dimensions `[marketplace]`; metric slots for the overlap, funnel and frequency metrics, each with its `period_grains` and the `time_grain` exact selector |
| `presentation/pages/v2/pages.yaml` | `mode: selectable`, `boundary_required: true`, `navigation: {hub_id: reach, tab_label: Reach and funnel, tab_description: How many shoppers your ads reached, how often, and what they did next, order: 1}`. `temporal_policy`: `candidate_presets: [last_complete_week, last_complete_month, last_complete_quarter]`, `default_preset: last_complete_month`, custom disabled, `comparison_policy: prior_calendar_period`, `display_grains: [declared_period]`. Artifact `ReachAndFunnelArtifact` v1, outcomes `[complete, empty, insufficient]` |

`prior_calendar_period` compares the same metric, contract and grain set across two periods, the one
cross-delivery comparison that's allowed (D-8 (a)).

| Section | `declared_periods` | `period_anchor` | Text on presets it doesn't declare |
|---|---|---|---|
| `workspace-scope` | none (every preset) | n/a | n/a |
| `overlap` | `last_complete_week`, `last_complete_month` | true | "Overlap is measured for complete weeks and months. Choose Latest measured month to see it." |
| `funnel` | `last_complete_week`, `last_complete_month` | true | The same pattern |
| `reach-insights` [proposal] | none: binds findings, not metrics | n/a | Each slot reads its own silence or absence |
| `also-changed` [proposal] | none: binds findings, not metrics | n/a | Skipped when empty |
| `frequency` | `last_complete_month`, `last_complete_quarter` | false | "Frequency is measured for complete months and quarters. Choose Latest measured month to see it." |
| `evidence-trace`, `boundary` | none (every preset) | n/a | n/a |

`declared_periods` holds calendar preset ids [repo: `src/domain/presentation/page_schemas.py:178`].

At the quarter preset no anchor section is bound, so `frequency` anchors there. The rule: if no section bound
on a preset is an anchor, every section bound on it anchors. Every grain word and preset label in these texts
is built on the server from `period_grains` and the declared preset labels
[repo: `src/domain/presentation/calendar_presets.py:306`].

### `ReachAndFunnelArtifact` v1

| Section | Content | Rendering responsibility |
|---|---|---|
| Overlap | `overlap_share` (headline, `account` IDs), `overlap_share_all_ids` beside it, `dsp_only_reach_share`, and `shoppers_reached` per exposure group; each with its prior-period value and server `change` block. The overlap object carries server-owned `all_ids_label: "All ID types"` for the `all_ids` group column; tile labels remain metric-specific. | Three tiles, a three-part bar with a table equivalent and an exact-values alternative, the window note |
| Funnel | Per exposure group on `account` IDs: `search_rate`, `purchaser_rate`, `revenue_per_exposed_shopper`, `purchaser_rate_index_vs_sponsored_only` | Caveat block, then the table, whose first column reads "Search rate" |
| Frequency curve | `purchaser_rate_by_frequency` per bucket (`account` IDs), with the bucket scheme the delivery used | Bar chart from a zero baseline, "Show values" table; a `not_returned` bucket draws a gap with "Not available" |
| Return per channel by frequency | `roas_credited_incl_halo_by_frequency` per channel and bucket band, pinned to `linear_v1`; the platform view `roas_incl_halo_by_frequency` beside it | Table with no total row, the foot sentence, and the platform line |
| Insights | Fixed roster `reach-overlap`, `full-funnel-association`, `frequency-saturation` with slot states | Insights row |
| Evidence status | `EvidenceStatus.v2` | Coverage sheet |
| Page context and starter questions | As the other tabs ([ADR-120](../../decisions/adr-120-carry-page-context-to-chat-as-a-server-redeemed-reference.md)) | Assistant chip and starter buttons |
| Boundary | Fixed statements: complete periods only, `account` IDs for rates, association not cause | "About these figures" |

**Bucket bands [proposal].** The approved mockup shows return per channel in three bands: 1 to 3 ads, 4 to 9
ads, and 10 or more. The server builds each band from pack 7 credited revenue and window spend summed across
the band's buckets within one ad product and one delivery. A band with any `not_returned` bucket reads "Not
available". The browser never adds buckets.

The business outcomes design doesn't allow this sum yet. D-3 and D-9 make a sum exact only across **every**
value of a disjoint axis in one delivery, with every value returned, and a band is a subset of the
`frequency_bucket` axis. For an additive measure the subset sum is still exact, because each shopper's window
falls in exactly one bucket. The bands map onto both bucket schemes: fine `1, 2, 3 | 4..9 | 10_plus` and
coarse `1, 2, 3 | 4_6, 7_9 | 10_plus` (business outcomes design §5.5). So S3.2 records the exception as an
amendment to D-9 and
[ADR-101](../../decisions/adr-101-model-shopper-groupings-as-disjoint-axes-with-one-window-per-shopper.md)
before it builds the bands: additive measures only, one ad product, one delivery, and every bucket in the
band returned [proposal]. Until that amendment is recorded, S3.2 doesn't build the bands.

### Labels and caveats (server text)

| Figure | Label |
|---|---|
| Saw both | "Saw both within 14 days of their first ad · Amazon account IDs · a lower bound: DSP impressions Amazon couldn't link to an account aren't matched" |
| Saw both, all ID types | "Saw both within 14 days of their first ad · all ID types · overlap is understated" |
| All-ID overlap table column | "All ID types" |
| Saw DSP only | "Saw DSP only within 14 days of their first ad · Amazon account IDs" |
| Overlap subtitle | "Shoppers reached in {period label}, grouped by the ads they saw within 14 days of their first ad in that {grain}." |
| Window note | "These groups use each shopper's 14-day window, so they don't match Amazon's own period reach reports." and "Ads seen before the {grain} began aren't counted." |
| Index column | "may understate the difference between groups" |
| Per-channel return | "Credited revenue ÷ spend · linear model · 14-day lookback from each shopper's first ad · includes brand halo · {period}" |
| Funnel column for `search_rate` (revised at `2026-10-09.2`) | "Search rate". Never "branded search", because AMC's documentation doesn't say these searches are branded (§10) |

The **caveat block** sits inside the full-funnel and frequency visuals, above the chart, never collapsible:
"This shows association, not cause. Shoppers were not randomly assigned to groups.", "Shoppers can see an ad
after they buy, so groups that include DSP retargeting may look stronger." and "Sponsored ads appear when
shoppers search, so sponsored groups start closer to buying." The frequency block adds "Shoppers can see
more ads after they buy, so higher buckets may look stronger." It also renders at the top of any insight or
focus dialog whose subject is a full-funnel or frequency figure. Starter questions and "Ask the assistant"
questions on this tab use "associated with", never "drove", "caused" or "led to".

The per-channel table's foot reads "Attributed sales aren't added across channels, because one purchase can
be credited to more than one channel."

### Designed states

| Condition | What renders |
|---|---|
| Latest measured week | Overlap and funnel for the week; frequency shows its absence text with "Show Latest measured month" |
| Period not landed yet | Page-level: "{period} isn't measured yet" with "Figures for {period} are expected after {date}."; no scope band; the preset button reads "{period}, not measured yet" |
| Period didn't arrive | Page-level: "Figures for {period} didn't arrive. The latest available is {latest}."; the preset button reads "{period}, didn't arrive" |
| Pack 7 without its credit branch | Every per-channel cell reads "Not available per channel" with "one purchase can follow ads on several channels, and we can't split its credit fairly here. See the platform view.", and the platform figure renders |
| Instant-access AMC instance (no DSP) | Not page-level: overlap and funnel read "Needs DSP and sponsored ads on one AMC instance"; sponsored-side frequency renders; the scope band still names the period |
| Fewer than 4 active campaigns | Frequency reads "Needs at least 4 active campaigns" |
| A suppressed cell or bucket | "Not available" with "too few shoppers, or none", never 0 |
| No published body for the preset (page-level) | Page-level absence: [ABS-REACH-1](../page-absence-copy.md#abs-reach-1), with [ABS-ALT](../page-absence-copy.md#abs-alt) when another preset is published; no scope band |

### Insight contracts (new)

| Contract | Class | Finding and rules |
|---|---|---|
| `reach-overlap` v1 | perspective | DSP-only shoppers as a share of the total, `account` IDs. The sentence names the ID space and the window. Calibration rejects presenting the account overlap as exact rather than a lower bound |
| `full-funnel-association` v1 | perspective, `causal: false` | Group comparison on `account` IDs. Calibration rejects "caused", "drove", "lifted", "incremental", any figure outside the findings, a DSP-versus-sponsored-only credit for search, and a rate quoted from the device-or-browser space |
| `frequency-saturation` v1 | perspective, `causal: false` | Names bucket *b* only when both steps *b*→*b*+1 and *b*+1→*b*+2 pass the upper-bound test on returned `account` buckets with enough shoppers and purchasers; silent next to any `not_returned` bucket; policy values `N_min`, `P_min`, `τ` and z versioned in the contract |

Every arithmetic step is plain domain code from the Python standard library.

### "Also changed"

The owner deferred the alert evaluator (OD-7), so this tab uses the alerts-declined path. The section lists
baked deterministic findings at or above a governed alert threshold for the current period, direction-only,
and each row about a full-funnel or frequency figure carries "This shows association, not cause." The
default rules ([ADR-108](../../decisions/adr-108-raise-in-app-alerts-when-page-artifacts-publish.md)) cover
`network-scorecard` and retail findings only, so in v1 this tab has no qualifying finding and the section
isn't rendered. Adding a rule over a `reach-overlap` finding brings it in with no page change. The same
gap applies to the Overview: its approved alerts-declined state draws a Reach row in "What changed", which
v1 can't produce without that rule. The Overview package (UI and information architecture slice 10)
records it.

### Materializer

New, under the ADR-075 pattern. It bakes on pack 5, 6 and 7 publication transitions, one artifact per
resolved period and preset, and bakes its deterministic findings.

## 7. Concepts applied

- [Business outcome measurement](../../concepts/business-outcome-measurement.md)
- [Preset artifact publisher](../preset-artifact-publisher.md)
- [Hybrid temporal-control contract](../temporal-control-contract.md)
- [Insight presentation contract](../insight-presentation-contract.md)

## 8. Decisions

- [ADR-099: distinct shopper counts as a new kind of fact](../../decisions/adr-099-treat-distinct-shopper-counts-as-a-new-kind-of-fact.md)
- [ADR-100: serve periodic metrics only for complete measured periods](../../decisions/adr-100-serve-periodic-metrics-only-for-complete-measured-periods.md)
- [ADR-101: disjoint groupings with one window per shopper](../../decisions/adr-101-model-shopper-groupings-as-disjoint-axes-with-one-window-per-shopper.md)
- [ADR-102: association-only flag and caveat](../../decisions/adr-102-flag-association-only-metrics-and-require-their-caveat.md)
- [ADR-106: outcomes inside one network](../../decisions/adr-106-keep-outcome-metrics-inside-one-network-by-type.md)
- [ADR-116: one time model and one window per tab](../../decisions/adr-116-give-every-tab-one-time-model-and-one-window.md)

## 9. Delivery prerequisites and acceptance

| Concern | Required proof |
|---|---|
| Page declaration | v1 and v2 entries pass `make validate-page-schemas`, including the per-section `declared_periods` and the quarter anchor rule |
| Periods | Unit tests on `resolve_calendar_preset` with fixture publications: pack 5 published and pack 6 not gives pending frequency and overlap with figures; week and quarter absence texts |
| Caveat | A page-text snapshot test finds the caveat block inside the funnel and frequency visuals and at the top of their dialogs |
| Per-channel return | `roas_incl_halo_by_frequency` refused at channel grain; the no-credit fallback renders every cell "Not available per channel" with the platform figure |
| Bucket bands | The D-9 and ADR-101 subset-sum amendment is recorded first; planner tests: a band sum inside one ad product and one delivery plans, a band with a `not_returned` bucket is refused and reads "Not available", and a band sum across ad products or deliveries is refused |
| Identity space | Rates bind `account` only; all-ID reach carries its unit |
| Insights | Three contracts with calibration corpora, including the noisy 30-purchaser dip that must stay silent |
| Conformance | Rule 4 of the [delivery slices](../information-architecture/delivery-slices.md#2-rules-for-every-slice) against every mockup state; states the dev data can't produce are unit-proven only |
| Live | Deployed dev bake observed end to end and recorded in `validation/RESULTS.md`; real AMC figures wait on the probes |

## 10. Open items

| Item | What resolves it | Blocks |
|---|---|---|
| Search rate label (revised at `2026-10-09.2`, business outcomes design §5.5 fallback). The approved mockup heads the funnel column "Branded search rate", and the design names the metric `branded_search_rate`. AMC's conversions pages list `searchConversion` and call these events search conversions, but no page says they're branded searches [verified: AMC's conversions and conversions_all datasource pages, read 2026-10-09]. So the metric ships as `search_rate`, the column reads "Search rate", and no text on this tab says "branded" | Owner ratification of manifest revision `2026-10-09.2` (wave 2b stop item S-B1). If an AMC source later says `searchConversion` is a branded search, a later revision may restore the label | Nothing; the build uses "Search rate" |
