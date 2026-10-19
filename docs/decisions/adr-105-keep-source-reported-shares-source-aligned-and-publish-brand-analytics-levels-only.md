# ADR-105: Keep source-reported shares source-aligned and publish Brand Analytics levels only

Date: 2026-10-06
Status: Proposed. The owner approved the business-outcomes design on 2026-10-06. Optional source: Brand
Analytics access is a customer onboarding prerequisite. Partly implemented (2026-10-08): the contract
gate that refuses a source-reported ratio or share in a curated column exists; contract 16 doesn't. The
current design is in
[Business outcome measurement](../concepts/business-outcome-measurement.md); source facts stay in
[Vendor Central](../sources/vendor-central.md).

## Context

Amazon's Brand Analytics repeat-purchase report (`GET_BRAND_ANALYTICS_REPEAT_PURCHASE_REPORT`,
`docs/sources/vendor-central.md:45`) gives, per ASIN and report period, orders, unique customers and repeat
purchase revenue. It also gives two shares Amazon computed: the repeat-customer share and the repeat-revenue
share [verified 2026-10-05, SP-API analytics report types page, published field list]. We can't rebuild those shares from the levels we hold, because Amazon's unique-customer count isn't
additive and its repeat-customer count isn't in the report.

The data-contract steering says "never store a ratio", without saying whether that covers the source-aligned
layer. [ADR-044](adr-044-materialize-source-aligned-data-and-centralize-canonical-publication.md) says the
source-aligned layer may not erase or relabel source meaning (its Decision, lines 48–49). The forecasting design reads
both the same way: ratios a source returns stay source-aligned.

The report offers WEEK, MONTH and QUARTER periods only [verified 2026-10-05, SP-API analytics report types
page], and its customer counts are non-additive. The `DAY` value listed at `docs/sources/vendor-central.md:109`
belongs to the Vendor sales and traffic reports, not to this one. So the vendor steering's rule to land DAY
and derive coarser periods can't apply here.

There is also no deployed landing path for Vendor reports today: they reach the raw zone only through the
dev fixture lander (`src/adapters/jobs/land_fixtures/bundle.py:112-113`).

## Decision

**The canonical fact holds Brand Analytics levels only. Amazon's reported shares are kept only in the
source-aligned table and are never recomputed or turned into a count.**

1. **Canonical fact.** `fct_repeat_purchase_reported` holds `orders`, `unique_customers` (reported,
   non-additive, never summed across products or periods) and `repeat_purchase_revenue` with its currency,
   at ASIN × report period × marketplace, basis `retail_actual`, explicit periodic.
2. **Shares stay source-aligned.** The two shares map only to the source-aligned table. They're never mapped
   to a curated column, never recomputed and never multiplied into a count. Where they're shown, they're
   evidence labelled "reported by Amazon", read from the source-aligned row.
3. **The reading this relies on.** "Never store a ratio" applies to curated canonical facts and to ratios we
   could derive. A non-derivable share a source reports is preserved only in the source-aligned layer,
   because ADR-044 forbids erasing source meaning. This is an inference from the two texts. No steering edit
   is proposed; a wording change needs the owner's approval.
4. **Each report period is its own option set.** `reportPeriod` is identity, in the partition key and the
   option set, so WEEK, MONTH and QUARTER reports don't trip the overlap check. No period is derived from
   another. The vendor DAY rule doesn't apply, for the reasons above.
5. **Landing.** The report lands through the forecasting design's governed API operation pack and
   generalised Finalizer (its ADR-089; the number is provisional and may change when the forecasting
   design renumbers its ADRs), with create-report as `read_job`, get-report and download
   as `read`, a pinned source-scope namespace and `watermark_policy: required`. The producer writes through
   its own landing role limited to `incoming/vendor_central/*`, under the forecasting owner's decision D6-3.
6. **Not a cohort path.** The report has no cohort month, follow-up month or vintage, so it never fills the
   cohort grid of [ADR-104](adr-104-define-acquisition-cohorts-by-amazons-new-to-brand-flag.md), and it's
   never joined to AMC data.

The levels render on [Repeat purchase by product](../ui/repeat-purchase-by-product/README.md), a tab of the
Customers hub; Retail health and Retention link to it (manifest revision `2026-10-08.1`, approved under the
owner's UI/IA approval of 2026-10-07).

## Consequences

- This outcome depends on the forecasting design's operation-pack ADR being implemented. Until then it can't
  land in a deployment.
- The report's array shape, timezone, revenue shape and percent scale are [unverified] until pinned from the
  published SP-API JSON schema when the contract is written.
- If the schema has an aggregate array, a second contract reads it with the reported-total pattern.
- The gate (`no-source-reported-ratio`, `check_no_source_reported_ratio_is_mapped` in
  `src/domain/data_contracts/validation.py:852`) refuses any curated mapping whose source names carry a
  ratio word (`pct`, `percent`, `rate`, `roas` and similar). The forecasting design curates a level as the
  product of a ratio and a level from one record (its ADR-088 `product_of`, for example usage percent ×
  budget), which this gate would refuse. Whichever design lands second makes the gate treat a `product_of`
  mapping as a level, with a paired test. Flagged to the forecasting workflow.

## Alternatives considered

- **Store the shares in the curated fact.** Conflicts with the steering's ratio rule and invites someone to
  multiply a share into a count.
- **Drop the shares.** Erases source meaning ADR-044 says to keep.
- **Derive weeks from days.** The report has no DAY period, and its customer counts can't be summed.

## Review trigger

Reopen when the owner changes the steering's ratio wording, or when the report gains a DAY period.
