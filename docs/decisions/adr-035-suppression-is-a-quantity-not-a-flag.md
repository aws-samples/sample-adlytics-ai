# ADR-035: Suppression is a quantity, not a flag

Date: 2026-08-21
Status: Accepted

> **Proposed amendment, 2026-10-06.** [ADR-099](adr-099-treat-distinct-shopper-counts-as-a-new-kind-of-fact.md) (Proposed) would have a distinct-count answer report missing cells, never a withheld quantity. This decision stays in force, unchanged, until ADR-099 is accepted, and afterwards for every other answer.

Closes [G-30](../decision-coverage-registry.md#gap-register-g-nn). Implements the reporting tiers [Publication State](../capabilities/publication-state/README.md) and [answerable-questions](../answerable-questions.md) §5 already specify, and replaces the blanket refusal in the Athena query adapter.

## Context

**38 of 50 declared metrics cannot be answered**, including every ROAS, every cost-per, `ctr`, `viewable_rate` and all reach metrics. `AthenaQueryAdapter._suppression_for` refuses any plan whose fact declares suppression columns, before composing anything.

**The refusal is about capability, not data** — proven rather than assumed. Pinned to `rows_suppressed = false`, where the query provably cannot read a withheld row, it still refuses; the plan carries identical `suppression_columns` either way, because that tuple comes from the contract rather than from any row (`tests/integration/test_suppression_refusal_scope.py`, [verified 2026-08-21]).

Its reasoning is sound: the honest aggregation is that a group containing a withheld row is not complete, and answering `rows_suppressed=False` without looking would be a claim rather than an absence. What is wrong is the granularity. It is a blanket applied per *fact* where the answer is available per *query*, and it collapses three cases into one refusal:

| Case | Honest answer | Today |
|---|---|---|
| Nothing withheld in this window | the figure, `rows_suppressed=False` **earned** | refused |
| Some rows withheld | the figure plus the withheld amount | refused |
| A whole group withheld | no figure for that group | refused |

The first is the common case. Our contracts map only NONE and LOW columns, so the bar is two distinct users and suppression is rare at a campaign × day grain.

**Four defects sit behind the refusal**, each currently unreachable and each a wrong answer the moment it is lifted.

The single-fact composition ignores the discriminator entirely and passes it through as an ordinary `WHERE` predicate — the exact "filter it away before anything can see it" mistake `_compose_side` was written to avoid. `_suppression_for` reads only `plan.suppression_columns`, which for a drill-across is the *numerator's*, so `roas_blended` (vendor numerator over AMC denominator) passes the gate and returns `rows_suppressed=False` while the denominator folds a real flag — a silent false claim today. `_side_value` returns a non-zero side's value regardless of the flag, so a partially withheld side reports as complete. And a withheld row is dropped into `undefined_rows`, which the narrative explains as a zero denominator.

## Decision

**Suppression is reported as a measured quantity, and `rows_suppressed=False` becomes something the query establishes rather than something an adapter asserts.**

### Three aggregates per side, not one

```sql
SUM(CASE WHEN NOT <flag> THEN <measure> ELSE 0 END) AS value_0        -- attributable
SUM(CASE WHEN     <flag> THEN <measure> ELSE 0 END) AS withheld_0     -- how much was withheld
SUM(CASE WHEN     <flag> THEN 1        ELSE 0 END) AS withheld_rows_0 -- whether, and how many
```

All three earn their place. The **quantity** is what makes a narrowed-evidence sentence ([provenance-chain](../concepts/provenance-chain.md)) expressible — *"a further 457 impressions came from locations Amazon withheld"* — and a boolean never could. The **count** is needed because a withheld measure can legitimately sum to zero: a suppressed row with no clicks contributes `0` to `withheld_0`, and without the count that is indistinguishable from nothing having been withheld. The count is also what a rate is computed from.

Applied on **both** paths. The single-fact branch has no fold at all today; the drill-across has one that computes `MAX(flag)` and discards the withheld measures.

### The discriminator leaves the `WHERE` clause, and stops being the caller's problem

Expressed in the aggregates on every path. A consequence follows that is worth stating, because it removes a wart: [ADR-024](adr-024-plans-account-for-every-grain-column.md)'s accounting check requires every grain column to be grouped or filtered, and `rows_suppressed` is in the grain of all three AMC facts with no dimension name — so every AMC question has had to arrive carrying `filters={"rows_suppressed": "false"}`.

**The discriminator now counts as accounted for whenever its fact declares suppression.** Splitting a measure into an attributable half and a withheld half is not summing across a grain column, which is what ADR-024 forbids; it is the opposite, and it accounts for the column more completely than a filter did. `LENS_DEFAULTS` was right to decline to default it — the fix was never a default.

### Partially withheld is reportable; wholly withheld is not

A group with some rows withheld reports its attributable figure **and the amount withheld beside it**. That is what [answerable-questions](../answerable-questions.md) §5 requires of any breakdown where suppression is material, and it matches Amazon's own guidance on the aggregation-threshold page: judge the proportion and decide, rather than discard the answer.

A group whose every row was withheld yields **no figure**. Its attributable sum is zero and that zero is a fabrication — it would read as a campaign that wasted its budget when it may have been the best performer. `_side_value` already decides this correctly for a drill-across and keeps doing so; what changes is that the flag it consults becomes a count.

Which tier a *grain* falls in — report silently, report with the rate, or withdraw the grain — stays [ADR-019](adr-019-grain-viability-is-measured.md)'s and is not built here. This decision makes the measurement that tier needs available for the first time.

### The reason is composed, never read from a column

`Suppression` requires a reason whenever `rows_suppressed` is true, and the column that would have supplied it was deliberately removed from every conformed fact ([G-28](../decision-coverage-registry.md#gap-register-g-nn)) because a per-delivery value has no meaning at a row several deliveries assembled. So the reason is built from what the query measured: how much was withheld, across how many groups, in the metric's own units.

That is also strictly better than the column would have been. `filteredReasonColumn`'s observed format is `1 sandbox.dsp_impressions users < 100 required` [verified 2026-08-21] — it names a data source and a shortfall, which is diagnostic detail about one delivery, not something a reader of a campaign breakdown can act on.

### The withheld residual does not join an answer yet

[ADR-034](adr-034-a-withheld-residual-is-keyed-on-its-delivery.md)'s residual is keyed on `contract × marketplace × period`, so it belongs to **no campaign** and cannot enter a campaign-grouped row. Adding it to a *total* is legitimate only where the requested grain is a subset of the residual's own, and that rule is a separate change with its own reads.

**Consequence, stated rather than discovered later: an answer's withheld quantity counts the fact's flagged rows and not the residual, so it under-reports.** Still a strict improvement on refusing outright, and recorded as an open item in the [gap register](../decision-coverage-registry.md#gap-register-g-nn).

## Consequences

**The 38 blocked metrics become answerable**, and the common case answers with `rows_suppressed=False` earned from a measurement rather than assumed.

**The refusal narrows rather than disappears.** A wholly withheld group still produces no figure, and a plan over a fact whose suppression columns the adapter cannot read still refuses. What goes away is refusing a whole fact because it *could* withhold.

**`roas_blended`'s silent false claim is fixed** by reading both sides' suppression columns. That was a live defect, not a gap.

**A withheld row and a zero denominator stop sharing a sentence.** Both leave a row without a figure and the reasons are opposite — one is missing evidence, the other is undefined arithmetic — so the narrative now distinguishes them.

**Six aggregates in a drill-across statement rather than four.** Each is a `CASE` over a column already scanned, so the cost is negligible beside the join it feeds.

**All fifty declared metrics plan without a pinned discriminator, and forty-eight execute.** [verified 2026-08-19] Completing the planning half took one further change: the non-additivity rule counted the discriminator as unaccounted-for even when the fold accounted for it, so `reach_users`, `cost_per_reach` and `avg_frequency` were the last three still requiring it. The fold is sound there for the reason filtering always was — a grain holds at most one unsuppressed row, so summing the attributable side sums exactly one distinct count, separating the two populations rather than adding them. It accounts for that one column and nothing else: a multi-day window still has to group by date. The two that do not execute are blocked on a currency crossing a join, unrelated to suppression — [G-35](../decision-coverage-registry.md#g-35--a-cross-fact-monetary-metric-cannot-state-its-currency).

**Pinning the discriminator is now wrong, not merely unnecessary.** With `rows_suppressed` supplied, ten metrics refuse — `fct_retail_sales` and `fct_product_page_views` have no such column, so naming it is an unfilterable-column refusal. A caller that kept the old habit would lose every Vendor Central metric.

**Executing all fifty surfaced two faults planning could not**, both fixed under [ADR-037](adr-037-a-union-table-has-no-per-row-invariant.md): a null measure on a fact several contracts write, and a monetary metric spanning two facts having no currency to state.

## Alternatives rejected

| Rejected | Why |
|---|---|
| **Keep `MAX(flag)` and report a boolean** | Cannot express any sentence the docs specify. "Some rows were withheld" gives a reader no way to judge whether the answer is usable, which `freshness-registry` §5.1 says explicitly |
| **Drop any group containing a withheld row** | Silently narrows a breakdown, and the narrative would have to explain a missing campaign rather than a qualified figure. Amazon's own guidance is to judge the proportion |
| **Report the attributable figure with no mention of withholding** | What `_side_value` effectively does today for a partially withheld side. A figure short by an unstated amount is the failure mode this platform exists to avoid |
| **Keep requiring the caller to pin `rows_suppressed`** | Makes every AMC question carry a filter that selects a different population, and leaves the discriminator accounted for by exclusion rather than by aggregation |
| **Read the reason from `suppression_reason`** | The column is gone from conformed facts (G-28), and its value names one delivery's data source rather than describing a group |

## Review trigger

Revisit when the residual joins an answer, because the withheld quantity then has two sources and the grain rule that decides when the residual applies has to be stated.

Revisit if a contract ever maps a MEDIUM or HIGH column. The bar becomes 100 distinct users, suppression stops being rare, and the tier thresholds ADR-019 defaults to 5% and 20% would start firing on ordinary questions.
