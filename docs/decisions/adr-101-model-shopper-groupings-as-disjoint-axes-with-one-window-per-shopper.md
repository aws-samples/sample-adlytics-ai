# ADR-101: Model shopper groupings as disjoint axes summable only within one extraction, with one window per shopper

Date: 2026-10-06
Status: Proposed. The owner approved the business-outcomes design on 2026-10-06, including the
sign-independent window (DS6-1) and option (c) for per-channel return by frequency (DS6-2). Partly
implemented (2026-10-08): the point 2 `exposure_source_sets` taxonomy section exists; the four
disjoint axes and the `disjoint_within_extraction` axis rule exist in `src/domain/semantics/compatibility.py`,
with `sum_disjoint` refusing a gap or a second delivery (`src/domain/shopper_aggregates/disjoint.py`).
No pack emits a source set yet, and no dictionary metric or fact column uses the axes.
The current design is in [Business outcome measurement](../concepts/business-outcome-measurement.md).

## Context

Reach overlap, frequency, the full funnel and cohorts put each shopper into one group: "saw DSP only",
"saw sponsored ads only", "saw both"; a frequency bucket; an acquisition group. AMC computes the group per
shopper inside the clean room. Four problems follow.

- **Sums.** Shopper counts can't be summed in general
  ([ADR-099](adr-099-treat-distinct-shopper-counts-as-a-new-kind-of-fact.md)). But a total across groups is
  wanted, and it's exact only when each shopper sits in exactly one group of one delivery.
- **Timing.** If the group comes from every touch in the period but outcomes only from 14 days after the
  first ad, a DSP retargeting ad seen after a purchase moves the shopper into "saw both" with that purchase.
- **Identity.** `user_id` mixes account, device and browser IDs (`docs/sources/amc.md:129`). One person
  seen on DSP under a device ID and on sponsored ads under an account ID counts as two shoppers, one in each
  "only" group.
- **Time arithmetic.** `SECONDS_BETWEEN(t1, t2)` is documented (`docs/sources/amc.md:396`), but whether it
  returns t2 − t1, t1 − t2 or an absolute value is [unverified]. A bare `SECONDS_BETWEEN(earlier, later) <
  1209600` bound is always true under t1 − t2, so the 14-day limit and the one-hour rule would silently fail
  (design review DS6-1). P-BO-1 Q0 settled it on 2026-10-08: t2 − t1 (decision 5).

Per-channel return by frequency had one more defect (DS6-2). Each ad product's frequency row counted any
ad-attributed purchase in the shopper's window, so one purchase sat in every channel's numerator, while the
denominator was one channel's spend.

## Decision

**Exposure group, frequency bucket, identity space and acquisition group are disjoint axes. A sum across all
values of one such axis is exact inside one delivery and refused otherwise. Every shopper has one window,
and every figure about that shopper comes from it.**

1. **Disjoint axes.** `exposure_group`, `frequency_bucket`, `identity_space` and `acquisition_channel_group`
   carry the axis aggregation `disjoint_within_extraction`. A sum across every value of one axis (or several
   together) inside one delivery (same contract, period, marketplace, grain set, and for frequency the same
   channel) is exact. If any value is `not_returned`, the sum is refused. No other axis qualifies.
2. **Source sets, then taxonomy.** Packs emit source-set labels (`dsp`, `sponsored`, `dsp+sponsored`). A new
   taxonomy section, `exposure_source_sets`, maps each to canonical keys within one platform
   ([ADR-052](adr-052-separate-ecosystem-platform-channel-and-ad-product.md)).
3. **One exposure fence.** The fence text is byte-identical in every pack that uses it, and a contract test
   compares it. Impression rows define exposure and first exposure. Sponsored click rows are kept only for
   their cost-per-click spend, so a click alone never starts a window or puts a shopper in a group.
4. **One window per shopper.** W runs from the shopper's first exposure hour in the period for 14 days.
   Group, frequency, identity space, impressions, spend and outcomes all come from W. Every overlap label
   names the window.
5. **Sign-independent window.** "After" is an ordering comparison of two hour columns only; the one-hour
   rule for searches and page views is `event_hour > first_exposure_hour`. "Within 14 days" is an absolute
   gap, written `GAP_SECONDS(a, b)` = `ABS(SECONDS_BETWEEN(a, b))`, always after the ordering test, so it
   holds under all three conventions. The earlier `CASE` form can't compile: AMC rejects its unary minus,
   and `ABS` is in the AMC SQL function reference and returned the gap (P-BO-1 Q0, 2026-10-08;
   [results](../../validation/RESULTS.md#business-outcome-probes-p-bo-1-step-a-and-p-bo-2--2026-10-08)).
   Probe P-BO-1 query Q0 reads the function on two fixed timestamps an hour apart, and criterion 8b gates
   the exposure, frequency and cohort packs on the recorded convention (t2 − t1, passed 2026-10-08).
6. **Frequency buckets** are a declared closed scheme, fine (`1` to `9`, `10_plus`) or coarse (`1`, `2`,
   `3`, `4_6`, `7_9`, `10_plus`), with no `ELSE` branch. A pack version uses one scheme. When any slice of a
   delivery passes ADR-019's degrade threshold, the coarse version is scheduled for that marketplace from the
   next period. A comparison across the two schemes is refused with its reason.
7. **Identity space in the grain.** Each cross-table pack emits `identity_space` (`account` when the shopper's
   `user_id_type` is `adUserId`, else `device_or_browser`). Every full-funnel and frequency rate, and every
   cohort rate compared across acquisition groups, reads `account` only. The account overlap is labelled a
   lower bound.
8. **Per-channel return by frequency uses credited revenue.** A channel's ratio divides only the revenue
   credited to that channel under the deduplicated attribution of
   [ADR-055](adr-055-govern-temporal-attribution-as-an-exact-selector.md) by that channel's window spend,
   inside one delivery. A model must be pinned. Any-ad revenue over one channel's spend is never served.
   Return by frequency on any-ad revenue is served at platform grain only. If the credit branch fails its
   probe or its runs, every channel cell reads "Not available per channel" with its reason, beside the
   platform figure.

## Consequences

- Overlap figures aren't comparable with Amazon's own period reach reports, because groups come from each
  shopper's window. Labels say so.
- The existing deduplicated attribution pack uses a bare `SECONDS_BETWEEN` bound
  (`contracts/amc/query-packs/amc-deduplicated-temporal-attribution.sql:87-88`). If Q0 records any convention
  other than t2 − t1, that pack's window is wrong too, and it goes to the owner as a separate defect.
- Frequency here counts impressions in the 14 days from first exposure. Amazon's optimal-frequency playbook
  counts impressions before the conversion [verified 2026-10-05, AMC optimal frequency playbook]; that basis
  would need its own pack and label.
- Credit rows are three per cell (one per model) and carry fewer purchasers, so they're withheld more often.
  A withheld credit cell reads "not available".
- Tie-breaking inside one hour for first and last touch follows a rule, not exact time, because only the
  hour columns are read. Linear credit doesn't depend on ties.

## Alternatives considered

- **Groups from every touch in the period.** Lets post-purchase ads move shoppers between groups.
- **A bare signed bound.** Correct only under one unconfirmed convention.
- **Credit from AMC's attributed-events tables.** Each row pairs a conversion with the traffic event it's
  credited to (`contracts/shared/amc-columns.yaml:1982`), and the tables' column lists have no conversion ID
  (`amc-columns.yaml:1995-2815`, `:2836-3656`). So one purchase may be credited to several campaigns and ad
  products, and credit can't be shown to sum to 100% [inference].
- **Rename the any-ad ratio and label it non-causal.** Readers would still compare it across channels as
  return. The owner chose credited revenue instead (2026-10-06).
- **All-ID rates.** A device or browser ID often can't match a purchase recorded under the account ID, so
  DSP-only rates would read low.

## Review trigger

Reopen when criterion 6b fails, or when AMC exposes a cross-ID-space shopper key. (Q0 recorded the
`SECONDS_BETWEEN` convention on 2026-10-08, and `ABS` replaced the `CASE`.)
