# ADR-106: Keep outcome metrics inside one network by type

Date: 2026-10-06
Status: Proposed. The owner approved the business-outcomes design on 2026-10-06, including that spend may
be totalled across networks and attributed outcomes never are. Implemented (2026-10-08): the dictionary
field, `AggregationPolicy`, `derive_source_platforms` and the planner's per-operand accounting exist;
no second network is onboarded.
Gives a typed form to the declared mixed-basis ratio of
[ADR-052](adr-052-separate-ecosystem-platform-channel-and-ad-product.md). The current design is in
[Business outcome measurement](../concepts/business-outcome-measurement.md).

## Context

The owner's rule: insights are per network on day 1. Spend may be totalled across networks within one
marketplace and currency. Attributed sales and outcomes are never summed across networks, because each
network credits a sale under its own basis and window.

Nothing in the metric typing enforces that today. The `ecosystem` and `platform` axes default to `additive`
(`contracts/shared/metric-dictionary.yaml:44-51`). A naive fix, making them `exact_only`, would break
today's plans. When a request leaves out an axis, the planner treats it as a roll-up and requires it to be
accounted (`src/domain/measurement/planning.py:868-912`; `src/domain/semantics/compatibility.py:190-211`).
So "attributed revenue last week", every campaign ordering and `tacos` at its only grain
(`[date, ecosystem, marketplace]`) would be refused.

## Decision

**A network is an advertising platform. Each simple metric declares how it aggregates across networks, and
the planner accounts the platform axis per operand from the platforms its sources can come from.**

1. **Typed field.** Every metric under `metrics:` declares `ecosystem_aggregation: additive | exact_only`,
   applied to the `ecosystem` and `platform` axes and enforced by `AggregationPolicy`. `ad_spend` is
   `additive` within one marketplace and currency. Every other metric is `exact_only`, impressions and clicks
   included. A metric without the field fails loading. `modelled_metrics:` entries aren't planned through
   these axes and aren't covered.
2. **Source platforms are derived, not authored.** A pure domain function, `derive_source_platforms`, walks
   each simple metric to the source packs publication selection can pick (the same match predicate:
   fact read and source dataset, any source system), then through the taxonomy to platforms. A media path
   resolves to every platform ever mapped in its namespace, regardless of `valid_from`. The set comes from
   governed files, never from loaded data.
3. **Per-operand accounting.** An operand whose source platforms have exactly one member has the platform
   and ecosystem axes accounted, because it can't be summed across platforms. Ratios check each leaf operand
   separately; there's no ratio-level policy.
4. **Mixed-basis ratios.** `tacos` (ad spend over Vendor shipped revenue) passes: spend is additive, and the
   revenue operand is single-platform. This is the typed form of ADR-052's declared mixed-basis ratio.
5. **When a second network lands,** its contracts add a platform to the metrics they feed. An omitted
   platform then becomes a typed refusal that names the axis, decided at load time.

## Consequences

- Every existing single-platform plan is unchanged. A regression guard runs the existing planner suites
  unchanged, and compares the axis check for every metric at every subset of its grains with the rule as it
  stood before (`tests/unit/test_source_platforms.py`).
- A composite's own `ecosystem_aggregation` records the outcome only: `additive` exactly when every operand
  is, which the catalog checks at load. It never drives planning.
- When an operand can come from more than one platform, the planner checks the omitted platform axis first,
  so the refusal names `platform`.
- A cross-network spend row can show figures only once a second network is loaded; until then it shows which
  networks are loaded and no figure.
- The same rule holds inside one network where one purchase can be credited twice. A simple metric may
  declare `exact_only_axes`, media or product grains it never sums across, so the planner refuses a
  total across them (design §6.2). The New customers gateway metric
  `purchases_new_to_brand_incl_halo_12mo` declares channel and ad product, so an all-channel product total
  is refused.
- The Scorecard's `NetworkScorecardArtifact` v1 holds attributed figures only on one channel of one
  network, and totals spend only inside one marketplace and currency
  (`src/domain/presentation/scorecard.py`).
- The forecasting design's new metrics under `metrics:` need the field too. Whichever design lands second
  adds it to its own metrics.
- Shopper facts can't hold a cross-network shopper anyway: AMC instances are isolated
  (`docs/sources/amc.md:452`).

## Alternatives considered

- **`exact_only` on ecosystem and platform alone.** Refuses most of today's plans.
- **Author `source_platforms` by hand.** Drifts from what selection actually reads, so a second network's
  contract could be read without being counted.
- **A policy per ratio.** Would let one ratio's declaration hide an operand's rule.

## Review trigger

Reopen when a second advertising network is onboarded, or when publication selection starts matching on
`source_system`.
