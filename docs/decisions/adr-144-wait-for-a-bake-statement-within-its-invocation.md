# ADR-144: Wait for a bake statement within its invocation
Date: 2026-10-18
Status: Proposed (2026-10-18). The owner approved on 2026-10-18 (UTC) a baker batch wait bounded by
the remaining Lambda time, replacing the 30 s interactive abandon, plus a start-time retry on Athena
throttling. On the same date the owner approved chat's throttling separately (option 1): a chat
or presentation start that Athena throttles retries for up to 10 s, inside chat's 30 s wait.

## Context
The baker builds the same Athena adapter chat uses. That adapter abandons a statement after 30 s, a
limit chosen to stay under the API Gateway integration timeout. A queue-driven bake Lambda has a
900 s timeout and no gateway.

On dev no bake statement has come near 30 s. The longest single execution, total time, was 11.2 s
(run `31e79634`, 2026-10-17); the longest per-query engine time across 28 runs was 16.2 s; and every
one of 7,480 bake executions in 48 h succeeded. A large seller's AMC queries may not stay under
30 s. One abandoned statement fails three receives and, today, withholds every page on the graph
entry, or dead-letters its page on the legacy entry.

Separately, a statement start over the account's active-query quota raises Athena's
`TooManyRequestsException`, and the adapter fails it at once.

The baker has two entries over one composed Athena adapter: the graph task, which only the
project's dev environment can provision, and the legacy entry, which every other deployment runs
(ADR-142). Whatever wait the adapter composes, both entries get it.

## Decision
1. **Two wait policies, chosen by each composition.** Every Athena target names its policy; there
   is no default.
   - Interactive callers keep the 30 s wait.
   - The baker uses a policy whose deadline is the task's remaining invocation time minus the row's
     tail and write terms from the bake contract. A per-task context value carries the deadline,
     and a bake statement without one is an invariant failure.
   - The remaining time comes only from the Lambda context. Both entries take it as a required
     argument and hand it to the one page execution they share (ADR-142 decision 7), which sets
     the deadline. On the legacy entry the record handler passes it, with whether this is the
     record's final receive, and the entry builds the page's window itself after it has parsed
     the intent and found its roster row. A declared page with no bake source runs no statement,
     so it gets no window; a statement outside the page execution would still be an invariant
     failure. The unbounded fallback is deleted. An invocation whose context has no
     remaining-time function is refused as a defect before any record runs, whatever its records
     are, never run without a deadline.
   - No bake, on either entry, reaches the 30 s abandon.
2. **Half an invocation is reserved for writes, and enforced at runtime.** Every row's write term,
   certified at its body bound, must fit 450 s (half the 900 s Lambda timeout). The build gate
   checks it, so a statement that waits to its deadline still leaves time to write. Because the
   write term rests on a budgeted time per object, the stage also stops submitting writes once
   less than the tail time remains, and that page settles its designed absence (ADR-142) instead
   of the invocation timing out.
3. **Throttled starts wait, briefly.** Under the bake policy, a `TooManyRequestsException` at start
   is retried with full-jitter exponential backoff (0.5 s base, 8 s cap) for up to 60 s, and never
   past the deadline. Under the interactive policy the same backoff retries for up to 10 s. The
   30 s deadline is fixed before the start, so the retries come out of it and at least 20 s remain
   for the statement. Every other start error fails at once, as today. Each task's summary counts
   its throttled attempts.
4. **A deadline timeout is the page's absence at once.** The timeout carries the policy name. On
   whichever receive it occurs, it's the page's isolated refusal (ADR-142), not a retry: the
   statement already had the invocation's whole statement window, and a repeat would delay every
   page's publication by a visibility timeout plus another window.

Decided by rubric item (4) of
[delivery practice](../delivery-practice.md#5-the-decision-weighting-rubric): an interactive limit
on a batch caller fails correct work, and the cost of the fix is one parameter.

## Will be enforced by
- `tests/unit/test_athena_execution.py`: throttled starts retried within the jitter bounds; past
  60 s or the deadline, a typed dependency error; another error fails at once; no retry without a
  throttle window; the interactive window retries for 10 s and the bake's for 60 s, and a throttle
  outlasting chat's 10 s fails (the inverted control).
- `tests/unit/test_athena_query_preflight.py`: each policy's limit; the policy in the timeout
  context; no deadline is an invariant; a target without a policy doesn't construct.
- `tests/unit/test_baker_composition.py`: the baker composes the bake policy.
- The baker handler's tests: a graph task and a legacy record each receive the context's own
  remaining-time function, and a legacy bake statement waits under the bake policy; an invocation
  without the function is refused once, every record fails, and nothing is consumed.
- `tests/unit/test_page_bake.py`: the deadline arithmetic for two rows, no leak between pages, and
  no bake statement outside the page execution.
- The stage's tests: the stage and the legacy publish both require the remaining time.
- `tests/unit/test_preset_bakeability.py`: every row's write term fits the allowance.

## Consequences
- A slow bake statement completes instead of failing the task. It holds one of the baker's 10 query
  slots longer, and the task may run past its budget, which alarms (ADR-142).
- A burst over the account's query quota queues for up to a minute instead of failing.
- In the worst case a page's bake takes up to about 47 minutes over three receives. On the graph
  entry that's cumulative within a lane, because a lane's tasks run one after another, and pages
  publish together, so every page's publication waits for the slowest lane; past the 7,200 s
  protocol ceiling the existing stuck-flow alarm fires. On the legacy entry it delays only that
  page. No page's content is cut.
- A chat question whose statement start is throttled waits up to 10 s for a start instead of
  failing at once. A sustained overload still fails the question within the 30 s, as before.

## Alternatives considered
- **Keep 30 s and rely on retries and result reuse.** Rejected by (4): it's nondeterministic, and
  three abandons still fail the task.
- **Retry a deadline timeout on the next receive.** Rejected: nothing suggests a repeat finishes,
  and it delays every page.
- **Chunk every statement to be provably short.** Rejected: it adds plans everywhere and can't help
  one inherently slow scan.
- **Use the existing 600 s batch wait.** Rejected: the invocation's remaining time is the real
  ceiling, and a fixed 600 s can outlive a late statement's invocation.
- **Give the legacy entry the interactive wait.** Rejected: it would keep the 30 s abandon on the
  only bake path most deployments run.

## Review trigger
A move of the baker off a 900 s invocation (for example, Lambda Managed Instances). A bake
statement measured near its deadline at a real seller. Or a measured write time that pushes a row's
write term past the allowance.
