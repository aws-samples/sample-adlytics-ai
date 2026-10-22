# ADR-102: Flag association-only metrics and require their caveat

Date: 2026-10-06
Status: Proposed. The owner approved the business-outcomes design on 2026-10-06. Not implemented.
The current design is in [Business outcome measurement](../concepts/business-outcome-measurement.md).

## Context

The full-funnel outcome (E1) compares search, page-view and purchase rates across shoppers who saw
DSP only, sponsored ads only, or both. The groups aren't random. Shoppers who see both are likely already
closer to buying, and sponsored ads appear when a shopper searches. So a higher rate in one group shows
association, not cause. Frequency readings ("the purchase rate stops rising after N impressions") have the
same limit.

The metric dictionary already carries one typed flag the narration layer must surface, `mixes_basis`
(`contracts/shared/metric-dictionary.yaml:20`). Nothing types "association only". Without a type, the
caveat depends on copy someone remembers to write, and a chat answer or insight could say "DSP drove
purchases".

## Decision

**A metric whose figures show association, not cause, carries `causal: false` in the dictionary, and every
surface that shows it carries the caveat.**

1. `causal: false` sits beside `mixes_basis` on every full-funnel metric, on the purchaser-rate index, and on
   the frequency readings. The default is `true` (no flag), so no existing metric changes.
2. The caveat sentence ("This shows association, not cause. Shoppers were not randomly assigned to groups.")
   is part of the visual block wherever the figures render, including any summary of them.
3. A chat answer that uses a `causal: false` metric adds a mandatory qualifier, "association, not cause",
   checked by evaluation like other mandatory qualifiers.
4. The insight contracts over these metrics reject causal wording in calibration: "caused", "drove",
   "lifted", "incremental", and a sentence that credits DSP for search by comparing a DSP group with
   sponsored only.

Amended 2026-10-22 (UI refresh 2.4, readout OD-UX-7, owner-approved): on the page, each visual shows one
caveat line, the served first line, which is the decision 2 sentence, and every served caveat line in one
"Why this is association, not cause" disclosure beneath it. Decision 2 still holds: the sentence stays
visible wherever the figures render. An insight or focus dialog over these figures still opens with the
full served caveat.
Amended 2026-10-09 (business outcomes wave 2b, plan D13): "branded search" became "search" in the context
and point 4. AMC's conversions pages don't say a `searchConversion` event is a branded search, so the
metric is `search_rate` ([Reach and funnel low-level design](../ui/reach-and-funnel/low-level-design.md#10-open-items)
§10).

## Consequences

- One more dictionary key the loader parses and narration reads.
- True lift within Amazon stays out of scope. These figures never stand in for an experiment.

## Alternatives considered

- **Caveat copy only.** Easy to omit on a new surface, and invisible to evaluation.
- **Don't serve the comparison.** Loses a useful reading the owner asked for (outcome E1), labelled
  "association, not cause".

## Review trigger

Reopen when a controlled lift source (for example an Amazon lift study) is onboarded.
