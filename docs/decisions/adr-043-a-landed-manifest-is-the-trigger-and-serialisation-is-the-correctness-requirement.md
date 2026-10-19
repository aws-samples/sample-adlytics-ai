# ADR-043: A landed manifest is the trigger, and serialisation is the correctness requirement

Date: 2026-08-24
Status: Accepted; manifest-trigger decision current, legacy publication mechanics superseded locally

> **Implementation amendment, 2026-08-24.** A landed manifest remains the load trigger. The V1
> serialisation and mutable-watermark implementation described as current below is historical in the
> local code and design: V2 fenced publication separates immutable attempts, expiring leases,
> immutable successes, `CURRENT` selection and exact coverage under
> [ADR-047](adr-047-separate-attempts-leases-publications-and-coverage.md). Deployment,
> forward/rollback/re-forward, live-publication, cutover, and V1-retirement acceptance remain pending
> in [validation results](../../validation/RESULTS.md#pending-acceptance--2026-08-24).
>
> **Source-acquisition amendment, 2026-08-24.** [ADR-050](adr-050-finalize-registered-cross-account-landings-into-governed-manifests.md) adds a separate `incoming/*/registration.json` event, registration queue/dead-letter queue, Finalizer and operational status table before this decision's manifest event. The Finalizer writes the canonical manifest last; this ADR's manifest rule, delivery queue and Semantic V2 handler remain unchanged in the local implementation. Registration finalization is not routed through or collocated with the Semantic V2 load function because their IAM and failure vocabularies differ. Read-only named-stack inspection and a producer-disabled no-change-set diff are complete. Producer-enabled deployment, producer conformance, and destination-readiness acceptance remain pending in [validation results](../../validation/RESULTS.md#pending-acceptance--2026-08-24).

**Implements the trigger [ADR-030](adr-030-the-raw-zone-landing-contract.md) implies.** That decision
made the manifest a delivery's completion marker and said its arrival is what starts a load. Nothing
acted on it: the load ran from a hand-invoked job, and `IngestionStack` was named in
[repo-structure](../repo-structure.md) as planned. This decides the chain between the two.

It also decides two things that turned out to matter more than the plumbing: what serialises a load,
and what a scheduled pull's window may look like.

Every claim carries a marker. `[verified]` means measured against deployed `dev` and the measurement
is named. `[unverified]` means it could not be settled and is recorded as open.

## Context

### The chain, and why each hop is there

```
raw bucket (FoundationStack, RETAIN)
  └─ NotificationConfiguration.EventBridgeConfiguration
       └─ EventBridge rule: bucket + suffix manifest.json
            └─ SQS standard queue (+ DLQ, maxReceiveCount 3)
                 └─ Lambda: reserved concurrency 1, batch size 1, timeout 900s
```

**EventBridge rather than an S3 notification, because the alternative is a cycle.** In the historical
V1 implementation, the retained raw bucket owner preceded the queue consumer in the manual sequence.
A direct S3-to-SQS notification would have made that owner need a queue ARN from a later deployment,
and SSM resolves at deploy so it cannot run backwards. [ADR-012](adr-012-stack-composition-and-config.md)
calls that a wrong boundary rather than something to work around. `EventBridgeEnabled: true` is a
boolean naming no consumer, so the bucket announces and whoever listens owns the rule. The current
retained raw boundary is owned by `DataV2Stack`; see the [manual deployment sequence](../../README.md#deploy).

**Written through the L1.** `eventBridgeEnabled` on the `s3.Bucket` L2 renders a custom resource — a
Lambda, a role and an `AWSLambdaBasicExecutionRole` managed policy calling `PutBucketNotification` at
deploy `[verified 2026-08-24 by cdk diff against Adlytics-Foundation-dev]`. `createDataBucket`
already refuses `autoDeleteObjects` for injecting a Lambda into a RETAIN data bucket; the Lambda
rules forbid a managed policy where an inline statement would do; and a CDK-managed function carries
a runtime CDK chooses, which is what the runtime pin exists to prevent. `AWS::S3::Bucket` has the
property natively, so the escape hatch renders one line and no compute.

**A queue between the rule and the function, and this is the load-bearing hop.** It buys three things
an EventBridge-to-Lambda target cannot: concurrency control, a redrive policy with an inspectable
backlog, and a dead-letter queue holding the message itself. Without it there is nothing to serialise
with.

**Standard, not FIFO.** A FIFO group key would have to be the target table, but an S3 key encodes
`source_system/source_dataset`, so deriving the table needs the contract registry and therefore a
router in front. Ordering is not what makes a delivery safe in any case: the watermark decision is,
and `SKIP_SUPERSEDED` exists so an out-of-order arrival is correct rather than tolerated.

### Serialisation is a correctness requirement, not a throughput choice

Reserved concurrency is 1, platform-wide. Three independent reasons and each is sufficient:

- Athena has no cross-statement transaction
  ([ADR-025](adr-025-applying-an-scd2-plan-without-a-transaction.md)), so a fact's DELETE-then-INSERT
  could interleave with another load of the same partition into a partition holding neither's rows.
- An SCD2 merge closed then inserted, so two concurrent dimension loads could close the same version
  twice.
- The legacy V1 path had no lease. `_apply` wrote `note(STARTED)` before merging, so a concurrent twin
  read `STARTED` and decided `APPLY_RETRY` — exactly right for a crashed predecessor and exactly wrong
  for a live one. **That ledger could not tell them apart.**

**This made serialisation a data invariant protected by a configuration value in V1, which was
weaker than it looked.**
A console edit, a raised quota, a manual invoke or a redrive running alongside live delivery all
reintroduce the interleaving, and the corruption is silent. The stack test asserts the property
because the property *is* the mitigation. The structural fix is a conditional lease in the ledger,
which changes what `STARTED` means and is recorded as open below.

The cost is nothing worth measuring: a delivery loads in 10 to 16 seconds
`[verified 2026-08-24, five timed loads against deployed dev]`.

### The 900-second ceiling is not structurally excluded

`BATCH_MAX_WAIT_SECONDS` in the load path is 600 **per statement**, and a fact issues roughly four
statements plus a dimension read. So a delivery can legitimately outlast Lambda's maximum. Measured
reality is far off that, but pretending the ceiling is safe would be the claim that stops being true
without anything failing.

What makes Lambda acceptable to be *wrong* about is the recovery: a timeout leaves the ledger at
`STARTED`, which is the `APPLY_RETRY` path, which redrive supplies. A fact write is a whole-partition
overwrite and an SCD2 merge is recomputed from current state, so repeating it is idempotent. The
failure mode of outgrowing Lambda is a retry, not data loss.

`ingest.delivery` now carries the duration, so moving to Step Functions becomes a measurement rather
than a guess. The active delivery queue records the same condition: add it only for a measured
retryable multi-step orchestration need, not as a speculative restructuring.

### A scheduled pull must re-pull fixed periods, never slide a window

This is the part a connector author will otherwise get wrong, because the correct reading of "a
rolling fourteen days" is not the obvious one.

A fact partition is identified by its **period bounds**. Two deliveries whose windows intersect
without matching are two distinct live partitions, and a question spanning them reads both:

| Shape | Result |
|---|---|
| Pull `08-01..08-14` today, `08-02..08-15` tomorrow | thirteen days counted twice |
| Pull `08-14..08-14` daily, and re-pull `08-01..08-13` as thirteen separate deliveries | each supersedes its own predecessor; nothing overlaps |

Both are "a rolling fourteen days on a schedule". Only the second is correct. The maturation window
becomes *how far back the schedule re-pulls*, which is a number in configuration rather than a
widening query.

**The option set is the other half of identity.** `options_hash`'s own docstring records that
`reportOptions` is dataset identity rather than a runtime flag, because a `MANUFACTURING` pull summed
with a `SOURCING` one double-counts. So changing a query's options yields a *new* partition rather
than a supersede: widen an attribution window and the re-pull lands beside the old data with both
live. A schedule must hold its options stable for as long as it intends to supersede, and an
intentional change is a migration with a decision behind it.

Nothing at load time caught the sliding-window shape. The query layer refuses half a dozen narrower
versions of the same arithmetic — `planning.py`, `answers.py`, `figures.py` and `comparison.py` all
decline to sum things that would overstate — while the load boundary had no equivalent. Given that
care, its absence read as an oversight rather than a decision.

### What self-heals and what does not

Four failure classes, not equally handled, and the summary "it is self-healing" would be false.

| Class | Handled | How |
|---|---|---|
| Transient — throttle, Athena timeout, function dies mid-merge | yes | message returns after the visibility timeout, ledger reads `STARTED`, `APPLY_RETRY`, load repeats idempotently |
| Duplicate event — S3 and SQS are both at-least-once | yes | identity is checked before watermark, so the same checksum returns `SKIP_ALREADY_LOADED` |
| Out-of-order restatement | yes | the older watermark returns `SKIP_SUPERSEDED`; redrive is therefore safe at **any** age |
| Poison delivery | no | split in two below |

A **rejected landing** — corrupt bytes, a byte count that does not match, a multi-object manifest —
raises, retries and reaches the dead-letter queue. Correct: nothing recorded a decision, and the
message naming the key is what is worth keeping.

A **quarantine** is decided and recorded, and is *not* dead-lettered. Replaying it would be refused
as `SKIP_ALREADY_REJECTED`, so it would spend its retries and then sit in the DLQ misrepresenting a
decision as undelivered work. It is announced by a metric and an alarm instead.

Which gives the rule the whole design follows: **fail the message when the delivery was never
decided; succeed it when it was decided and the decision is recorded.** The dead-letter queue is for
undelivered work, never for refused work.

### Nothing acquires data

The chain starts at a landed manifest. Nothing lands one. `src/adapters/outbound/` holds no AMC
client and no Vendor Central client `[verified 2026-08-24]`, and the only writer into the raw zone is
`land_fixtures`, whose own docstring says it honours the contract "it asks of a producer" — standing
in for a producer that does not exist.

So this decision makes `dev` ingest continuously from fixtures and an AMC probe, and makes a customer
deployment ingest nothing until a connector exists. That is worth stating rather than letting the
trigger read as completion: it also reframes the connectors as the next piece of work rather than
something further down a list, because the trigger is what gives them somewhere to deliver.

## Decision

1. **A landed manifest triggers a load**, through EventBridge, a standard SQS queue and a Lambda. The
   rule filters on the bucket and the `manifest.json` suffix.
2. **Reserved concurrency is 1 and batch size is 1**, and both are asserted. Serialisation is a
   correctness requirement; the retry unit equals the delivery unit.
3. **The visibility timeout is the function timeout plus a fixed minute.** The usual advice of six
   times assumes a short function; against a fifteen-minute one it puts ninety minutes between
   attempts and three hours before a dead letter, which is slower than a human noticing.
4. **Dead-letter membership follows decided-versus-undecided.** A rejected landing propagates; a
   quarantine returns success and is counted.
5. **A fifth load assertion refuses a fact delivery whose period intersects one already loaded** for
   the same contract and option set. Equality is deliberately not a violation: that is the
   restatement path. Facts only — a dimension merges by natural key, so intersecting windows produce
   validity history rather than a doubled measure.
6. **A scheduled pull re-pulls fixed periods** and holds its option set stable.
7. **The load path emits its own telemetry**, in `pipeline.py` so the CLI and the function share one
   implementation: `ingest.delivery`, `ingest.partition`, and `athena.statement` carrying
   `StatementOutcome`'s attributes.
8. **Two alarms and an SNS topic with no subscription.** The ARN is published for a customer to
   attach a destination to; one in the template would be our address in their account.

## Consequences

**A disposable stack with no reader.** The current manual deployment sequence is in the
[root README](../../README.md#deploy). `IngestionStack` is DESTROY throughout. The
delivery it reacts to is in the RETAIN raw bucket and the rows it writes are in the RETAIN lake, so
losing the stack loses the reaction and never the data; re-landing a manifest replays it.

**The function needs no `ssm:GetParameter`.** Every value it reads is a pure function of the
environment name, derived at synth. The CLI still reads the parameters, because a developer at a
terminal has none of the environment.

**`Settings` gains an empty-tolerant field.** `raw_bucket` follows the `harness_arn` precedent:
configuration resolves as a whole, so requiring a bucket only one function reads would stop the tool
Lambdas and the auth handlers. `IngestTarget` refuses an empty one, which is the boundary both
callers pass through.

**The first alerting in the platform.** No CloudWatch alarm existed anywhere in `infra/` before this
`[verified 2026-08-24]`. Two arrive here, and they are silent until a customer subscribes to the
topic — which is a smaller lie than a dead-letter queue nobody is ever told about, and is stated
rather than implied.

**The overlap assertion reads the ledger with a filtered scan.** The family prefix is on the hash
key, so `begins_with` has no sort key to work on. Acceptable at any volume this platform will see —
the ledger holds one item per partition, and `reset_load_state` already documents a scan here as
unavoidable — and a secondary index keyed on the family with the period as its range is the
escalation when item count justifies it, not before.

**The ledger now stores `period_start` and `period_end` as attributes.** They duplicate two segments
of the identity string deliberately, because the alternative is a second parser that has to agree
with `PartitionKey.identity` forever. An item written before they existed reads as unknown, which
cannot be claimed to overlap — the safe direction.

### Adoption-time open items and later resolution

**The conditional lease is implemented locally by V2.** At adoption, serialisation was configuration
rather than structure, and the proposed fix was a `note(STARTED)` that refused a live predecessor
with an expiry. ADR-047 superseded that mechanism in local code and design with separate expiring
leases and fencing tokens. Reserved concurrency is an operational throttle rather than the intended
correctness boundary. Deployment, live-publication, cutover, and V1-retirement acceptance remain
pending in [validation results](../../validation/RESULTS.md#pending-acceptance--2026-08-24).

**A quarantined delivery cannot recover.** `SKIP_ALREADY_REJECTED` refuses identical bytes, so a fact
that arrived before its dimension stays quarantined even once the dimension lands. Distinguishing
"this delivery violates its contract" from "its dimension is not here yet" would make that case
genuinely self-healing, and it widens `decide_load`'s outcome vocabulary.

**No row-count reconciliation.** A manifest declares a byte count and a digest, so a truncated object
is caught; it declares no row count, so a mapping that silently drops rows reports what it wrote and
nothing notices. Adding one amends ADR-030.

**The marketplace namespace split was a V1 defect and is superseded.** The former partition overwrite
deleted by the *manifest's* marketplace and inserted the *rows'*. Those differed for real AMC data,
so a restatement could append a second copy. V2's ADR-045 resolver and exact publication selection
replaced that write path; the defect remains recorded here because it helped force the identity
boundary.

## Alternatives considered

**Step Functions now.** Rejected because a statement-per-task state machine changes the *load path*, not the trigger, and the trigger is separable. Introduce it only for a measured retryable multi-step need approaching the observable duration ceiling.

**S3 to SQS directly.** Rejected for the dependency cycle above.

**FIFO.** Rejected because the group key needs the contract registry, and because ordering is not
what makes a delivery safe.

**Reserved concurrency above 1, with a lease.** The right eventual shape and the wrong order: the
lease is the hard part and shipping the higher concurrency first would make the corruption reachable
while it was being built.

**Dead-lettering a quarantine.** Rejected because a redrive of identical bytes is refused by design,
so the message would consume its retries and then misreport a decision as a delivery nobody handled.

**A per-contract quarantine alarm.** Rejected because a metric published with two dimensions is not
retrievable by one of them, so `Action` plus `Contract` would need one alarm per contract and would
silently miss the next contract added. The contract is on the span and in the log line, where
cardinality is free.
