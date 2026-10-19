# ADR-103: Model audience segments as a source-namespaced, overlapping dimension

Date: 2026-10-06
Status: Proposed. The owner approved the business-outcomes design on 2026-10-06, with open decision 5
(Persona Builder demographics fit the clean-room rule) on its default, yes. Not implemented.
The current design is in [Business outcome measurement](../concepts/business-outcome-measurement.md).

## Context

The audience outcome (C2 with C1) asks which Amazon audiences over-index among a brand's buyers, and which
DSP segments perform. Segment data comes from two places: DSP segment tables in standard AMC, and Audience
Segment Insights (ASI), a paid feature. They use different ID spaces. Whether a DSP segment ID equals an ASI
segment ID is unknown until probe P-BO-4 runs.

Three facts shape the model.

- A shopper belongs to many segments, so segment figures never add up.
- A segment roster is an open domain: segments come and go month to month, and small ones fall under AMC's
  privacy threshold.
- An over-index test runs once per segment, often hundreds per table. A fixed 95% bound would name about
  2.5% of segments by chance alone.

## Decision

**Segments are a dimension keyed by platform, source namespace, segment ID and segment marketplace. The
`audience_segment` axis is overlapping and never summed.**

1. **Dimension.** `dim_audience_segment` is keyed `platform_key × segment_namespace × segment_id ×
   segment_marketplace_key`, with SCD2 history. Namespaces are `amc_dsp_behavior` (standard) and `amc_asi`
   (paid).
2. **Never summed.** The `audience_segment` axis is `exact_only`. Additive measures on segment facts follow
   [ADR-099](adr-099-treat-distinct-shopper-counts-as-a-new-kind-of-fact.md): `exact_only` on segment.
3. **Every segment returned, no filter in SQL.** Packs return every segment of the table with fixed text and
   parameters, so nothing varies between runs. A load-side roster (last month's returned segments) marks
   missing ones "not available", never index 0. A new segment joins next month's roster. Roster loss is
   graded, never withdrawn, because no coarser view of segments exists.
4. **Bases.** The profile's denominators are profiled shoppers: exposed shoppers present in the segment
   table, and buyers among them, so buyers are a subset of exposed. They come from their own total row in
   the same delivery. If that row is missing, every share and index reads "not available".
5. **Naming a segment.** A segment is named as over-indexing only when it has at least `B_min` buyers
   (proposed 30) and the lower bound of buyer share minus exposed share is above 0 at a Bonferroni value,
   z = Φ⁻¹(1 − 0.025 ÷ S), where S is the number of segments in that delivery.
6. **Namespaces merge only after a probe.** DSP and ASI segments stay separate until P-BO-4 shows ID
   equality. No link between C1 and C2 is drawn before then. P-BO-4 step 1 failed on the validation
   instance on 2026-10-09
   ([results](../../validation/RESULTS.md#p-bo-3-and-p-bo-4-on-the-standard-demo-instance)), so they stay
   separate, and the Audiences page says the two use different IDs.
7. **Demographic segments** (open decision 5 = yes) appear like any other segment, labelled by their
   provider, Amazon or third party, like every segment (amended 2026-10-09 by the owner: P-BO-3 found
   demographic segments, some of them third-party [inference from the names], so "Amazon's" fits only part of them). If the owner ever reverses
   decision 5, a governed exclusion list applies at load, not in SQL.
8. **Paid path off.** Without ASI deliveries the paid view shows the
   [ADR-107](adr-107-derive-paid-feature-availability-from-delivery-evidence.md) state and the standard DSP
   path renders.

## Consequences

- The ASI packs can't ship until the integer `segment_marketplace_id` value is pinned in a governed file
  from Amazon's documentation, or from a single observed value under a narrow rule. The US value was pinned
  in `contracts/shared/commerce-identities.yaml` on 2026-10-08.
- Implemented in part (2026-10-11, not deployed): packs 9 and 13 write `dim_audience_segment`, pack 8
  writes `fct_segment_delivery` ([canonical model](../canonical-model.md#dim_audience_segment)). P-BO-4
  step 1 failed, so the two namespaces have separate writers and histories and no cross-namespace key.
- Every segment carries `provider` from the governed classification
  `contracts/shared/audience-segment-providers.yaml`: an exact (namespace, name) entry, then the namespace's
  rule (ASI: Amazon; DSP behaviour segments: none, because they mix providers). A segment no rule covers
  reads "Provider not recorded", never "Amazon". The entries are P-BO-3's first pass, accepted for dev on
  2026-10-09; a human review is pending.
- The first month has an empty roster, so it can't flag missing segments. The section says so.
- Bonferroni is conservative. A small real difference in a large table may go unnamed.

## Alternatives considered

- **One merged segment namespace.** Unsafe before P-BO-4 proves ID equality.
- **Filter the segments of interest in SQL.** A filter list that changes between runs is the pattern AMC's
  differencing guard rejects (`docs/sources/amc.md:171`).
- **Benjamini–Hochberg.** Needs a ranking step, and its guarantee is harder to state plainly.

## Review trigger

Reopen when P-BO-4 passes or fails, or when ASI documents a stable cross-table segment key.
