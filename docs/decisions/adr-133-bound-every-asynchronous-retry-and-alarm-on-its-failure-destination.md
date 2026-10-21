# ADR-133: Bound every asynchronous retry and alarm on its failure destination
Date: 2026-10-11
Status: Proposed (2026-10-09). It rests on the owner's approval, on 2026-10-09, of the workflow
retrospective's recommendations, which named this decision. It is not accepted: acceptance is the
owner's review of this file. Implemented in code on `feat/hardening-no-silent-failure`
(`8dff83dd`, `f2b1c245`, `6b91d002`) and not yet deployed, so its live checks on dev are pending.

> **Amended 2026-10-18 by [ADR-148](adr-148-hold-a-page-alarm-until-an-observed-success-clears-it.md)
> (Proposed), clause 4 refined.** Presentation page operations leave the operation catch-all and
> reach the staffed destination through per-route page alarms instead.
## Context
An asynchronous consumer that fails without a bound or an alarm fails silently: nothing pages, and
the work it holds is lost or blocks what follows.
- At base `5bdaa331`, five DynamoDB stream event source mappings set no retry bound, no record age
  or no on-failure destination: `ExecutionGraphStreamMapping` (bisect only)
  [repo: `infra/lib/stacks/operational-status-processor-stack.ts:385` at `5bdaa331`],
  `Stage1PublicationsStreamMapping` [repo: `infra/lib/stacks/operational-status-stage1.ts:140` at
  `5bdaa331`], `AuditKeysOnlyMapping` [repo: `infra/lib/stacks/agent-quality-processor-stack.ts:92`
  at `5bdaa331`], and the two `PublicationCurrentMapping`s
  [repo: `infra/lib/stacks/insight-precompute-stack.ts:113` and
  `infra/lib/stacks/publisher-stack.ts:312` at `5bdaa331`]. All five are bounded in `8dff83dd`.
- The attempts stream mapping retried a record it could never apply until the stream dropped it,
  holding the shard with no alarm. It was bounded, with an on-failure queue that pages, in
  `7286c09a`. A DynamoDB stream keeps records for 24 hours, and a mapping's default retry count and
  record age are both infinite (-1) [verified 2026-10-11,
  https://docs.aws.amazon.com/lambda/latest/dg/services-ddb-params.html].
- The attempts-exhausted alarm first watched standing depth, so after one fire it stayed in ALARM
  and hid the next exhaustion. `981548d0` changed it to signal arrivals. A queue's
  `NumberOfMessagesSent` does not count a message that reaches a dead-letter queue through a failed
  processing attempt, only one sent to it directly [verified 2026-10-11,
  https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/troubleshooting-dlq-redrive.html],
  so the arrival form also reads the growth of `ApproximateNumberOfMessagesVisible`.
- At base, six dead-letter queue alarms watched standing depth and three dead-letter queues had no
  alarm (`8dff83dd`).
- The rule that dependency and invariant failures alarm already existed as contributor guidance
  and was not followed. Prose could not hold it.
## Decision
**Nothing asynchronous fails silently: every retry is bounded, every failure has a destination, and
every destination and failing operation reaches an alarm on the staffed destination.**
1. A stream event source mapping sets a maximum retry count, a maximum record age, bisect-on-error
   and an on-failure destination.
2. An SQS consumer's queue has a redrive dead-letter queue.
3. Every dead-letter queue and failure destination alarms on arrival,
   `MAX([FILL(sent, 0), FILL(DIFF(visible), 0)])`, not on standing depth, to the staffed
   destination.
4. Every function alarms on its `AWS/Lambda` `Errors`, and every operation that reports an invariant
   or dependency failure through `report_error` reaches an alarm. A caught exception is re-raised,
   translated, reported or carried, and never discarded.
5. An alarm in ALARM or INSUFFICIENT_DATA past one deploy cycle carries a recorded disposition.

The limits come from configuration, never inline.

Decided by rubric items (1) and (5) of
[delivery practice](../delivery-practice.md#5-the-decision-weighting-rubric): an unbounded retry
or an unalarmed failure loses or blocks work with no signal, and an arrival alarm on the staffed
destination is the actionable signal. Item (2) chose the patterns: a bounded retry, a failure
destination and a dead-letter queue.

Arrival semantics extend to product outcomes that are not failures. A signal that means "this
happened again" alarms on its transition, and an expected outcome is never counted as a failure.
For example, an ingestion contract whose every bucket is older than its `CURRENT` counts
`IngestPartitions` `Action=superseded`, a product signal that never alarms, instead of `blocked`
[repo: `src/adapters/jobs/ingest_delivery/semantic_sidecar.py:2338`], added in `f2b1c245`. So
`ingestion-blocked` no longer stands in ALARM through a bulk load and hides the next real block.
## Enforced by
- Clauses 1 and 2, and clause 3 in part: `AsyncFailureBoundsAspect`
  [repo: `infra/lib/mixins/async-failure-bounds.ts`], which fails `cdk synth` and every infra test
  that synthesizes a violating stack. For clause 3 it proves the arrival form and that the alarm's
  actions are enabled and not empty. It does not compare an action with the staffed destination,
  so an arrival alarm wired to another topic passes it. `expectArrivalAlarm`
  [repo: `infra/test/support/async-failure.ts`] proves the destination only for the queues a stack
  test names; review owns every other queue.
- Clause 4, in part: `make check-alarm-coverage` (`infra/test/alarm-coverage.test.ts`) for function
  and operation alarms, and `make check-exception-handling`, part of `make check`, for caught
  exceptions. The exception gate examines only Python under `src/`, and there only a handler that
  catches a broad or alarm-class type by its written name. It checks that a propagating form is
  present, not that every branch propagates, so one raising branch passes a handler whose other
  branch returns. It does not examine an aliased import, a tuple constant or a typed built-in such
  as `ValueError` [repo: `scripts/check-exception-handling.py`]. Review owns those cases and every
  catch outside `src/`.
- Clause 5: `make dev-state`, which exits 3 on an alarm not OK or a non-empty failure queue that
  has no disposition.
- That the limits come from configuration: review. The aspect reads each mapping's resolved
  retry count and record age, after `stack.resolve`, where a typed read of `ASYNC_FAILURE_BOUNDS`
  [repo: `infra/lib/config/index.ts`] and an inline literal look the same. It proves a mapping is
  bounded, not where its bound came from.

The Python gate's tests carry `pytest.mark.covers("ADR-133")`. The two infra gates are Jest tests,
which have no `covers` mechanism, so the coverage matrix can't list them.
## Consequences
- The current rules, the one exemption from function `Errors` alarms and the operation catch-all
  alarm are owned by [observability §3.2](../concepts/observability.md#32-application-spans-and-metrics).
  What an operator runs for a failure queue is in
  [operator-tools](../capabilities/operational-data-flow-status/operator-tools.md#stream-failure-queues-attempts_failures).
- A poisoned stream record now leaves its shard after the configured bound instead of holding it
  for up to 24 hours. Its metadata goes to the failure queue, and the record itself is lost once
  the stream drops it, so the operator's window to read it is the stream's retention.
- New alarms will surface standing failures on their first deploy. Each needs a disposition before
  `make dev-state` passes.
- The `Adlytics` EMF namespace has no environment dimension, so the operation catch-all, like the
  existing ingestion alarms, counts every environment in the account.
## Alternatives considered
- **Steering prose only.** Rejected by (1): the alarm rule already existed in steering and was not
  followed.
- **Alarm on standing depth.** Rejected by (5): one message left in the queue holds the alarm in
  ALARM and hides every later arrival.
- **A hand-kept roster of alarmed operations.** Rejected by (1): a new operation is uncovered until
  someone remembers to add it. One catch-all alarm grouped by operation covers it on arrival.
## Review trigger
A consumer type the aspect does not model, such as Kinesis, Amazon MSK or an EventBridge Pipe, or a
failure destination that is not an SQS queue.
