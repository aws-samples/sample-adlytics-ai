# ADR-031: A control-total failure quarantines every grain the document writes

Date: 2026-08-20
Status: Accepted

A consequence of [ADR-030](adr-030-the-raw-zone-landing-contract.md)'s dataset-keyed fan-out that needed its own answer. Recorded separately because it is a decision about what an assertion *means*, not about where a file goes.

## Context

One vendor document is read by two contracts at two grains — `GET_VENDOR_SALES_REPORT` becomes `vendor-sales-by-asin` and `vendor-sales-reported-total`. Each writes a different table and gets its own run-ledger entry, so each can succeed or be quarantined independently. That independence is correct for three of the four load assertions: a header problem, a currency disagreement or an unresolved dimension key is a fact about one contract's rows.

The control total is not like the others. It compares one contract's detail rows against the *other* contract's reported aggregate, and it fails when they disagree. So the failure is not located in either contract — it is located in the relationship between them, and the assertion runs once per landing rather than once per contract.

That leaves a question the fan-out creates and nothing answered: when the sums disagree, which partition is quarantined?

The trap is that "quarantine the detail rows" sounds right, because a breakdown that does not add up looks like the broken half. It is not knowable. A disagreement is symmetric evidence: either a detail row is missing or wrong, or the reported total is wrong, and the check cannot distinguish them. Vendor Central publishes both from its own store, so either can be the defective one.

## Decision

**A control-total failure quarantines every partition the document would have written.**

Both grains, always, not the one that looks suspect.

## Consequences

**The blast radius is wider than the evidence.** One of the two figures is probably fine and we withhold it anyway. That is accepted deliberately, on the same principle [ADR-025](adr-025-applying-an-scd2-plan-without-a-transaction.md) settles for the SCD2 merge: **fail towards an absence rather than towards a plausible number.** An absent figure is visible, is asked about, and is repaired by the next delivery. A published figure whose own breakdown contradicts it is not detectable by anyone downstream, and it is worse in both directions — publishing only the total gives a reconciliation anchor that its own detail refutes, and publishing only the detail gives every product-grain answer a base whose own source disagrees with it.

**The last good partition stays live for both**, which is what makes the wider radius tolerable. A quarantine is not a deletion; yesterday's figures remain answerable and the freshness registry reports the staleness honestly.

**One violation, two ledger entries.** The assertion produces a single `Violation`, and the load records `QUARANTINED` against each contract's partition. The alarm fires once — it is one defect in one document, and firing twice would overstate how much is wrong.

**A recurring failure is a source problem, not ours.** If a document reliably fails, the escalation is to Amazon or to the producer that operates extraction (outside this repository), not to a tolerance. Which is the next point.

**Exact equality stays exact.** A tolerance would be the obvious way to shrink the blast radius, and it is refused: any tolerance is a guess about how wrong a figure may be while still being publishable, and nobody can state that number. Quarantining on a rounding difference is the safe direction to be wrong in, and it will surface as a loud, fixable defect in the mapper's scale handling rather than as a quiet one in the data.

## Alternatives rejected

| Rejected | Why |
|---|---|
| **Quarantine the detail grain only** | Reads as the intuitive answer and encodes an assumption the check cannot support — that the detail is the wrong half. Leaves a reconciliation anchor live that its own breakdown refutes |
| **Quarantine the total grain only** | The mirror error, and worse in practice: the detail grain feeds every product-level answer, so it is the half more likely to be read |
| **Publish both, flag the disagreement in the freshness registry** | Superficially the honest option, and it fails because a flag is not a refusal. The figures would be queryable, tools would return them, and the caveat would depend on narration surfacing a registry field that nothing forces it to read |
| **Accept a tolerance** | Requires stating how wrong is acceptable. Nobody can, and the number would be chosen to make a specific failing document pass |
| **Reconcile automatically by trusting the total and scaling the detail** | Fabricates rows to satisfy a check. The platform's core invariant is that no figure is invented |

## Review trigger

Revisit if a real document fails this assertion for a *documented* source-side reason — a known Amazon publishing lag between the aggregate and the breakdown, for instance. That would make the disagreement expected rather than evidence, and the right response would be to delay the load until both agree, not to relax the check.
