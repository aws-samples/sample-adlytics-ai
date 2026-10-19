# ADR-047: Separate attempts, leases, publications, and coverage

Date: 2026-08-24
Status: Accepted and implemented locally as the publication-state authority

> **Implementation amendment, 2026-08-24.** V2 locally implements separate immutable attempts,
> expiring leases with fencing tokens, immutable successful publications, `CURRENT` selection, and
> exact interval/bucket coverage. The V1 migration language below is retained as adoption history,
> not as a current authority statement. [Publication State](../capabilities/publication-state/README.md) owns the local
> serving model. Deployment, live-publication, reprocessing, cutover, and V1-retirement acceptance
> remain pending in [validation results](../../validation/RESULTS.md#pending-acceptance--2026-08-24).

> **Amendment, 2026-09-16 — publication is ordered by source revision, not only by fencing.**
> Live dev reproduction: an older-watermark vendor delivery, redelivered after its newer
> restatement had published (a raw-zone event retry arriving late), created a new publication
> and moved `CURRENT` *backwards* — the serving layer answered with the pre-restatement
> figure. Fencing tokens order competing writers; nothing ordered the source's own timeline.
> The rule this amendment adds: **a candidate whose source revision is strictly older than
> the incumbent `CURRENT` publication's terminates as a `skipped` attempt and never
> publishes.** An equal source revision still publishes — batch reprocessing legitimately
> republishes the same source state under changed rules — and a newer revision supersedes as
> before. The guard is applied per partition at publish decision time in the semantic
> sidecar, using the domain's `SourceRevision` ordering.

This replaces the former mutable partition record in the local code and design. The former record is
absent from the current local implementation; named-stack inspection, cutover, and V1 physical
retirement still require the pending acceptance above.

## Context

An attempt says that work ran. A lease says who may currently publish. A successful partition
publication says which validated version became readable. Coverage says which intervals those
readable publications establish. Collapsing those into one current-state record creates ambiguous
states:

- `STARTED` cannot distinguish a live worker from a crashed predecessor;
- an expired worker can still finish unless publication is fenced;
- a failed or quarantined attempt can overwrite evidence about the last successful publication; and
- one latest window per contract cannot represent disjoint intervals without either losing coverage
  or widening across holes.

[ADR-043](adr-043-a-landed-manifest-is-the-trigger-and-serialisation-is-the-correctness-requirement.md)
used reserved concurrency because the former path had no lease. [ADR-036](adr-036-coverage-and-freshness-are-two-properties.md)
kept reporting honest when that registry could not establish a common window. Those were correct
constraints for the adoption-time shape; implemented V2 separates the concepts rather than asking one
item to carry all of them.

## Decision

**Represent attempt state, leases, successful partition publications, and exact interval coverage as
four separate concepts with separate invariants.**

### Attempt

An attempt is an immutable record of one execution against one delivery and intended publication
scope. It records its attempt identity, delivery identity, start and finish instants, outcome, and
failure or validation evidence. Retries are new attempts linked to the predecessor; they do not
rewrite it. A skip, rejection, quarantine, failure, and success remain distinct outcomes.

### Lease

A lease is a conditional, expiring grant to publish one canonical scope. It records a holder, expiry,
and monotonically increasing fencing token. Acquiring or renewing it is independent of recording an
attempt. Expiry means only that the holder no longer has authority; it does not decide whether the
attempt succeeded or failed.

Every mutation that can make a canonical partition current presents the fencing token. A holder with
a stale token cannot publish even if it completes after a replacement worker. Serial execution may
remain an operational throttle, but it is no longer the correctness mechanism.

### Successful partition publication

A publication is an immutable record that a named canonical owner made one validated partition
version readable. It references its source-aligned inputs, delivery provenance, semantic and DQ
evidence, exact measurement interval or bucket set, and the fencing token that authorized it. A
restatement creates a new publication and explicitly supersedes the prior current version; it does
not erase attempt history.

Only successful publications can become current. A failed, rejected, skipped, or quarantined attempt
creates no successful publication and cannot remove the last good one.

### Exact interval coverage

Coverage is a conclusion derived from the current successful publications for one semantic dataset,
identity scope, option/lens set, and time class. It is represented as a normalized set of exact
intervals or buckets. Adjacent intervals may coalesce; gaps remain gaps. The minimum start and maximum
end are never a substitute for the set.

A requested interval is covered only when the union of current successful publications contains the
whole request under the relevant time semantics. A successful restatement replaces the interval
contributed by the superseded publication. Attempts and leases never extend coverage.

## Consequences

**Concurrency safety becomes structural.** A console change, redrive, or higher worker concurrency
cannot let an expired worker publish over a newer holder when every publication is fenced.

**Recovery no longer overloads status.** Operators can inspect every attempt while analytical readers
resolve only current successful publications and derived coverage.

**Coverage can represent holes honestly.** Two successful daily ranges separated by an unloaded day
remain two intervals, so a query spanning the gap is refused or qualified rather than reported as
fully covered.

**The state model and write path become more complex.** Append-only attempts, conditional leases,
immutable publications, current-version selection, and interval normalization require separate
storage and consistency rules. That cost replaces the present reliance on one mutable record and
configuration-level serialisation.

**Publication atomicity needs an explicit protocol.** Data must be committed and validated before the
publication record becomes current, and a failed current-pointer update must be recoverable without
claiming coverage. The owner of the load path will specify that protocol; this ADR fixes the states it
must preserve.

**The former registry limitations did not improve merely because the target was accepted.** During
migration, its authority and incomplete disjoint-window history remained as documented. That
migration is now resolved: V2 `CURRENT` and exact coverage are the sole serving authority.

## Alternatives rejected

| Rejected | Why |
|---|---|
| Keep one mutable ledger item and add more statuses | Status cannot provide mutual exclusion, fencing, immutable history, and exact coverage simultaneously |
| Treat `STARTED` as a lease | It has no holder authority, expiry, renewal, or fencing token, so a stale worker can still publish |
| Derive coverage from every completed attempt | Completion does not prove validation or publication, and retries would duplicate evidence |
| Store only minimum and maximum covered dates | Widens across holes and turns partial data into a claim of complete coverage |
| Rely permanently on reserved concurrency one | Configuration limits ordinary execution but does not fence manual invocation, redrive, or a stale worker |

## Review trigger

Revisit if the publication store provides a native transaction that atomically commits data, current
version, and interval coverage. The concepts remain separate, but the protocol and storage boundaries
may become simpler.
