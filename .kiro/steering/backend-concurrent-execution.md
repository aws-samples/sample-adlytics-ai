---
inclusion: auto
name: backend-concurrent-execution
description: Use when implementing or diagnosing concurrent execution in a request handler or job that performs multiple independent queries, port calls, or downstream requests, including fan-out bounds, deterministic joins, and timeout pressure.
---

# Concurrent execution of independent I/O

Port/adapter boundary rules are owned by meta-architecture-hexagonal.

## The rules

- When a handler executes more than one independent I/O operation — queries,
  port calls, downstream requests — execute them concurrently. Sequential
  execution of an independent batch is a defect, not a style choice.
- Bound every concurrent fan-out explicitly. The bound's value belongs to
  configuration or the downstream service's declared quota, never to a literal
  at the call site.
- Join concurrent results by position or identity, never by completion order.
  Parallelism must not be able to change an answer.
- Never fix an over-budget batch by raising a timeout. First make the batch
  concurrent; if it still cannot fit its budget, split the endpoint or
  precompute the artifact.
- Concurrency lives in adapters. Never make a domain function async, hand it
  an executor, or require an event loop to test it.
