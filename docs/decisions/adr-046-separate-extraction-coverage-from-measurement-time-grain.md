# ADR-046: Separate extraction coverage from measurement time grain

Date: 2026-08-24
Status: Accepted

This establishes Semantic Architecture V2 time semantics. It does not claim that future source packs,
query packs, or their declared fact classes are implemented.

## Context

A delivery window answers **what interval an extraction attempted to cover**. A row's date or period
answers **what interval one measurement represents**. Treating the first as the second makes a
fourteen-day extraction look like one fourteen-day measure, or makes a fourteen-day total look like
fourteen daily measures that can be joined and summed. Both errors return believable numbers.

The canonical model already distinguishes additive facts, non-additive measures, and snapshots, and
[ADR-004](adr-004-timezone-and-fx.md) preserves source business day. The load path also rejects
ambiguous overlapping fact windows. V2 needs the missing type boundary: extraction coverage is
provenance about an execution; measurement bucket and time grain are semantics about rows.

This matters most when a source returns a total for a requested period. The total proves neither a
daily allocation nor an event time. Dividing or copying it into daily rows fabricates information,
while storing it beside daily additive facts makes an ordinary `SUM` silently double count it.

## Decision

**Extraction coverage and row measurement time are separate typed values, and canonical facts declare
one of three time classes.**

1. **Daily additive fact.** Every row names one source business day, and its additive measures may be
   summed across distinct days subject to their other axis rules. An extraction may cover many days,
   but it publishes one daily bucket per represented day. Missing days remain missing; the extraction
   interval does not fill them.
2. **Explicit periodic fact.** Every row names the exact measurement period whose total it represents.
   It is not daily, cannot be sliced or prorated into days, and may be combined across time only when
   the metric's typed semantics permit a set of non-overlapping periods.
3. **Snapshot fact.** Every row names an observation instant or effective bucket and is not additive
   across time. Snapshot details remain owned by [canonical-model](../canonical-model.md).

Every source-aligned publication records both:

- the extraction interval claimed by the producer or manifest; and
- the measurement bucket or period carried by each row, including its source timezone or business-day
  basis.

Prepublication validation proves that row buckets lie within the extraction claim under the source's
time convention, without asserting that the extraction interval is fully populated. Coverage is
derived from successful publications under [ADR-047](adr-047-separate-attempts-leases-publications-and-coverage.md),
not from the requested extraction bounds alone.

A period total with no source-supported daily attribution publishes only to an explicit periodic
fact. It may not enter a daily additive fact, be repeated once per day, or be divided evenly over the
window. Ratios remain computed from their operands at a compatible time class; they do not turn a
periodic operand into daily evidence.

## Consequences

**Daily arithmetic becomes structurally meaningful.** A sum across daily additive rows represents a
union of daily buckets rather than a mixture of daily values and period totals.

**Some source outputs cannot satisfy daily questions.** The honest result is an explicit periodic
answer or a refusal, not a fabricated daily series. This may reduce apparent capability while
increasing semantic accuracy.

**Extraction coverage no longer implies row completeness.** A delivery requested for a month may
contain missing days, suppressed rows, or one periodic total. Exact answer coverage comes from what
successful publications establish, not from the request alone.

**Time compatibility becomes part of metric planning.** A daily operand and a periodic operand can be
combined only through a declared compatible operation. Calendar labels, source business days, and UTC
instants are not silently coerced.

**Migration requires classifying every canonical fact.** The classification belongs with its model
metadata and runtime semantics, not in explanatory notes. This ADR records the rule and does not
claim the classification or migration is complete.

## Alternatives rejected

| Rejected | Why |
|---|---|
| Use the extraction window as every row's grain | Confuses execution provenance with measurement semantics and makes multi-day extracts non-additive |
| Spread a period total evenly across days | Invents a distribution the source did not provide and makes downstream daily joins look exact |
| Store daily and periodic values in one additive fact with a nullable period field | Leaves ordinary aggregation able to sum incompatible rows |
| Forbid periodic facts entirely | Discards valid source evidence merely because it cannot answer a daily question |

## Review trigger

Revisit when a source supplies a verified allocation method that converts one supported periodic
measure into daily measures. The conversion must be source-grounded and metric-specific; a generic
proration policy does not qualify.
