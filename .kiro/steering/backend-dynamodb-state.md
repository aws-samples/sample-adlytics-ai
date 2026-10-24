---
inclusion: fileMatch
fileMatchPattern: ["**/src/adapters/outbound/dynamodb/**"]
description: Governs conditional writes, fencing monotonicity, lease transitions, conflict handling, and key-space ownership when editing or adding DynamoDB state adapters.
---

# DynamoDB state adapters

The existing adapters in this directory are the reference implementations;
docs/capabilities/load-path/write-discipline.md owns the monotonicity design.

## Every write states its condition

- Never issue an unconditioned put or update. A create asserts
  attribute_not_exists on the item key; a replacement asserts the exact state it
  believes it is replacing. An unconditioned write is a lost-update bug that no
  test will catch under unit concurrency.
- A key that must move forward enforces its fencing in the ConditionExpression —
  supersedes identity plus token comparison — never in read-then-write code. The
  condition is the lock; code around it is advice.
- A leased-work transition asserts both the state and the lease reference it
  owns, in one conditional write, so an expired lease cannot complete work it
  lost.
- Treat ConditionalCheckFailedException as a typed domain outcome (conflict,
  stale, already-applied), never as a retryable transport error. Retrying a
  failed condition without re-reading is the race it exists to prevent.

## Tables

- Item shapes and key prefixes are declared beside the adapter that owns them;
  never write another adapter's key space.
