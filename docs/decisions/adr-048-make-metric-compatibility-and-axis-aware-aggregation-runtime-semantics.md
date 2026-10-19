# ADR-048: Make metric compatibility and axis-aware aggregation runtime semantics

Date: 2026-08-24
Status: Accepted

This extends [ADR-032](adr-032-tools-are-shaped-by-operation-not-by-metric.md): operations remain the
tool surface, while metric meaning becomes typed input to planning.

> **Implementation amendment, 2026-08-24.** The typed compatibility signature, per-axis policies,
> ratio-of-aggregates planning, structured qualifiers, and runtime refusals are implemented and
> checked locally on the Semantic V2 target query path. [Canonical model](../canonical-model.md) owns
> current declarations and [query path](../capabilities/query-path/README.md) owns current execution design. Deployment,
> live-publication, and browser acceptance remain pending in
> [validation results](../../validation/RESULTS.md#pending-acceptance--2026-08-24). Accepted
> [ADR-052](adr-052-separate-ecosystem-platform-channel-and-ad-product.md) adds no queryable axis until
> its mapping and metric policies are implemented.

## Context

A metric dictionary can contain correct prose and still permit wrong execution. Notes such as “never
sum across time”, “marketplace only”, “mixes basis”, “requires one distributor view”, or “periodic”
help a reviewer but do not constrain a planner unless each is represented in a type the runtime must
handle. Ignored prose is documentation, not semantics.

The current design already proves parts of this direction: ratio operands determine valid-grain
intersection, plans account for every grain column, structural and measured valid grains remain
separate, and tools rather than the model perform arithmetic. V2 must generalize those rules from a
collection of special checks into one typed metric algebra.

An **axis** is any independent dimension across which values might be grouped, filtered, aligned, or
aggregated: time, platform, channel, commerce scope, marketplace, product, campaign, source option,
measurement basis, and any later governed dimension. A metric can be additive on one axis,
non-additive on another, and unavailable on a third. A single `valid_grains` list cannot state all of
those operations.

## Decision

**Metric compatibility and aggregation behavior are typed runtime semantics. Explanatory notes may
clarify a rule but may not be the only place that rule exists.**

Every metric declaration compiles into a typed specification containing at least:

- measure kind: additive, semi-additive, non-additive, snapshot, or ratio/expression;
- operands and the operation that combines them;
- units and scaling;
- available axes and, for each axis, whether the value may be grouped, filtered, aligned, and
  aggregated across it;
- time class from [ADR-046](adr-046-separate-extraction-coverage-from-measurement-time-grain.md);
- required selectors and source-option compatibility;
- measurement-basis and source compatibility, including whether a mixed result is permitted and
  which caveat it requires; and
- identity scopes required for alignment.

Unknown kinds, axes, compatibility modes, or aggregation behaviors fail declaration loading. A note
cannot introduce a new behavior that the typed specification does not express.

The planner builds a typed aggregation expression from that specification:

1. align operands only on compatible identity scopes, time classes, and shared axes;
2. aggregate each operand according to its own per-axis behavior;
3. combine aggregated operands, so ratios are always ratio-of-aggregates rather than
   aggregate-of-row-ratios;
4. preserve required selectors and caveats in the plan and shaped response; and
5. return a typed refusal when any requested axis or metric combination is incompatible.

Compatibility is evaluated for every operation involving more than one metric or fact, including
ratios, comparisons, rankings, totals, and future analytical operations. Structural compatibility
from the metric specification and measured availability from
[Publication State](../capabilities/publication-state/README.md) are both required and remain distinct.

[canonical-model](../canonical-model.md) continues to own metric definitions. [query-path](../capabilities/query-path/README.md)
owns planning and execution mechanics. [Conversational Runtime](../capabilities/conversational-runtime/README.md) owns the response envelope
and mandatory caveats. This ADR owns why each rule must cross those boundaries as a type.

## Consequences

**Illegal arithmetic becomes unrepresentable or explicitly refused.** A periodic value cannot be
silently spread across daily buckets, a reach value cannot be summed across an undeclared axis, and a
ratio cannot be averaged from row ratios merely because SQL permits it.

**The dictionary becomes an executable contract.** Adding a metric requires complete typed semantics,
and startup or validation fails if the runtime does not understand them. Free-form descriptions stay
useful for people but carry no enforcement authority.

**Planner complexity increases while tool count does not.** [ADR-032](adr-032-tools-are-shaped-by-operation-not-by-metric.md)
still bounds tools by operation. New semantic classes add planner behavior, not metric-specific tools.

**Compatibility refusals become explainable.** The runtime can name the incompatible axis, time class,
identity scope, basis, source option, or aggregation behavior and offer a compatible coarser question
without asking the model to infer the reason.

**Migration was required at acceptance.** Rules that lived only in prose had to become typed fields
before V2 could treat their metrics as publishable. Current migration and execution status are owned
by [canonical model](../canonical-model.md) and [query path](../capabilities/query-path/README.md), not by this ADR's
historical acceptance text.

## Alternatives rejected

| Rejected | Why |
|---|---|
| Keep semantic rules in descriptions and review them manually | Review cannot constrain runtime behavior, and the failure returns plausible figures rather than an error |
| Use one `valid_grains` list for every operation | Availability at a grain does not say whether a metric is additive, alignable, filterable, or comparable across that axis |
| Encode special cases in SQL templates | Duplicates semantics per operation and makes compatibility invisible to catalog and narration |
| Let the model decide whether metrics are compatible | Moves deterministic arithmetic and data-model rules into inference, outside tests and runtime enforcement |
| Compute ratios per row and aggregate the result | Changes weighting and produces a different metric from the declared ratio of operands |

## Review trigger

Revisit when a required analytical operation cannot be expressed as operand alignment, per-axis
aggregation, and deterministic combination. The new operation must extend the typed algebra rather
than introduce an untyped escape hatch.
