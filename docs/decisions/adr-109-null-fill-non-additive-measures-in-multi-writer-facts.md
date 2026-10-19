# ADR-109: Null-fill non-additive measures in multi-writer facts

Date: 2026-10-06
Status: Proposed. The owner approved the business-outcomes design on 2026-10-06. Implemented in the
repository (2026-10-09): the union-schema generator declares `reach_users` null-filled, and
`amc-dsp-reach` is the fifth `fct_ad_exposure` writer. Not deployed.
Narrows the zero-fill rule of the union-schema generator for non-additive columns, and returns DSP distinct
reach, which the 2026-08-24 amendment of [ADR-022](adr-022-contract-decomposition-by-dataset.md) removed,
as its own writer. Related: [ADR-037](adr-037-a-union-table-has-no-per-row-invariant.md). The current
design is in [Business outcome measurement](../concepts/business-outcome-measurement.md).

## Context

`fct_ad_exposure` is a union: several contracts write it, each supplying only its own columns
([ADR-037](adr-037-a-union-table-has-no-per-row-invariant.md)). The union-schema generator zero-fills a
column absent from one contributing contract (`src/adapters/jobs/schema_generation/merge.py:12-16`). That's
right for additive measures: a clicks contract that writes no impressions adds zero impressions.

The deduplicated-reach outcome (D1) adds DSP reach per campaign-day, a distinct count, as a new writer of
`reach_users`. It's its own contract so a suppressed reach delivery can't block DSP spend. Under zero-fill, a
campaign-day with spend but no reach delivery would read reach 0, which claims nobody was reached.

## Decision

**In a fact several contracts write, a column whose every writer declares it `non_additive` is filled with
NULL, not zero, where a writer doesn't supply it.**

1. The union fill is NULL for a column every writer declares `non_additive: true`. `SUM` ignores NULL, so a
   campaign-day with a DSP reach delivery reads its reach, and one without reads NULL, which renders "not
   available".
2. Additive columns keep zero-fill.
3. DSP reach is its own `fct_ad_exposure` writer (`amc-dsp-reach`, policy `reject_delivery`, like sponsored
   reach).
4. The `_withheld` residual and the control-total check exclude non-additive measures
   ([ADR-099](adr-099-treat-distinct-shopper-counts-as-a-new-kind-of-fact.md)).

## Consequences

- Forecasting reads `fct_ad_exposure` spend, which stays zero-filled, so its inputs don't change.
- A reach figure is served only for one campaign on one day, because reach is extracted per campaign-day.
  Other scopes read a plain "not available" reason.

## Alternatives considered

- **Keep zero-fill.** Turns "no reach delivery" into "reached nobody".
- **A separate reach fact.** Splits one conformed exposure grain into two tables for one column.

## Review trigger

Reopen when another non-additive measure joins a union fact with a different absence meaning.
