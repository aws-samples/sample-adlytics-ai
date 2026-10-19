# ADR-104: Define acquisition cohorts by Amazon's new-to-brand flag

Date: 2026-10-06
Status: Proposed. The owner approved the business-outcomes design on 2026-10-06. Implemented in code
(2026-10-11, not deployed): packs 14 and 15 write `fct_shopper_acquisition` and its residual, with the
calendar-quarter variant switching on automatically, and the Retention server side waits on its page
entry. Their first live AMC runs are pending.
Specifies the declared-but-empty `fct_shopper_acquisition` row of the [canonical model](../canonical-model.md)
and changes its declared source. The current design is in
[Business outcome measurement](../concepts/business-outcome-measurement.md).

## Context

The repeat-purchase outcome (B3) asks whether shoppers won by ads come back, by month since acquisition and
by how they were won. The canonical model lists `fct_shopper_acquisition` as blocked on AMC paid features
(`docs/canonical-model.md:483`). Four facts constrain a cohort design.

- AMC ad data covers about 13 months, and `user_id` stability over time is undocumented
  (`docs/sources/amc.md:151`). A "first ever purchase" can't be defined inside that window.
- Amazon flags each attributed purchase as new-to-brand over its own 12-month lookback
  (`docs/sources/amc.md:92`).
- The standard repeat source, `conversions`, holds only ad-attributed and pixel events, so it sees only
  ad-touched repeats (`contracts/shared/amc-columns.yaml:1452`). The paid Amazon Retail Purchases (ARP)
  table sees all Amazon purchases, but only under account IDs (`adUserId`;
  `amc-columns.yaml:3751-3754`).
- Cohort counts are distinct counts
  ([ADR-099](adr-099-treat-distinct-shopper-counts-as-a-new-kind-of-fact.md)), so cumulative repeat buyers
  can't be summed across months.

## Decision

**A cohort is the calendar month of a shopper's first new-to-brand purchase in the window, as Amazon flags
it. Groups, follow-up months and vintages are fixed so each cell has one population.**

1. **Acquisition.** A shopper's acquisition date is the earliest purchase in the attributed table flagged
   `new_to_brand`. The cohort month is its calendar month.
2. **Acquisition group.** The credit sources on that day give one disjoint group per shopper: won by DSP
   only, sponsored ads only, or both. It's an
   [ADR-101](adr-101-model-shopper-groupings-as-disjoint-axes-with-one-window-per-shopper.md) disjoint axis.
3. **Follow-up months** 1 to 12 inside one 13-month window. Month 0 is excluded. "After" is an ordering
   comparison, so a purchase before acquisition can never count.
4. **Two paths, never one series.** Standard: repeats from `conversions`, basis `ad_attributed`, labelled
   "Ad-touched repeat purchases · bought again from this advertiser". ARP (paid): repeats from ARP, cohort
   restricted to `adUserId` on both sides, basis `retail_actual`, rates declared `mixes_basis: true`. The
   ARP rates divide ARP repeat buyers by the standard pack's account-ID cohort size for the same vintage and
   cell, and are withheld with a plain reason when an integrity check shows the cohort changed between runs.
5. **Cumulative repeat buyers are extracted** as their own distinct count, never summed across months.
6. **Cross-group rates read account IDs.** Repeats are matched across tables by `user_id`, so a device or
   browser ID can match only a purchase under the same ID. Any rate compared across acquisition groups reads
   account-ID repeat measures over account-ID cohort size, labelled "Amazon account IDs". All-ID rates stay
   for one group on its own.
7. **Vintages.** Each monthly run recomputes the grid as an `as_of_month` vintage, an exact selector that
   defaults to the latest vintage with a current publication.
8. **"From this advertiser".** On a multi-brand instance, new-to-brand is per brand, so a shopper won for one
   brand who later buys another counts as a repeat. Labels say "from this advertiser".

## Consequences

- The standard path understates true repeat, and says so. ARP lifts that only for enrolled customers.
- The standard pack becomes the first writer of `fct_shopper_acquisition`, ARP an optional second. The
  canonical model now specifies the fact and no longer lists it as blocked on AMC paid features
  ([canonical model](../canonical-model.md#fct_shopper_acquisition--domain-shopper_lifecycle)).
- The ARP rates pair two deliveries, so no single query computes them. Chat and the Retention bake both pair
  the operands per cell under the integrity check, through one domain rule (owner decision 2026-10-12). Without a
  pack 15 delivery for the vintage only the ARP rates are refused; the standard path never binds pack 15.
- Each monthly run re-reads about 13 overlapping months, which is exposed to AMC's differencing guard. The
  cohort packs are in the shed order: once the weekly variants and quarterly frequency have been dropped,
  they fall back to quarterly vintages.
- Whether Amazon's 12-month new-to-brand lookback reaches past the 13-month instance retention is
  [unverified].

## Alternatives considered

- **"First purchase in the window" as acquisition.** Wrong at the window's start, where every shopper looks
  new.
- **Brand Analytics as a cohort path.** It reports per product and period, with no cohort month or vintage,
  so it can't fill a triangle ([ADR-105](adr-105-keep-source-reported-shares-source-aligned-and-publish-brand-analytics-levels-only.md)).
- **Sum monthly repeaters into a cumulative curve.** Double-counts shoppers who repeat in more than one
  month.

## Review trigger

Reopen when Amazon documents `user_id` stability beyond 13 months, or when ARP covers non-account IDs.
