---
inclusion: auto
name: meta-performance
description: Use when designing, reviewing, or diagnosing user-facing request performance, including latency budgets, revision-keyed caching, publication-time precomputation, or response and fetch payload size.
---

# Performance discipline

Concurrent execution of independent I/O is owned by backend-concurrent-execution and is not
restated here.

## The rules

- Every user-facing request path declares a latency budget in its owning design doc before
  implementation begins. A change that adds time to a path states where the time comes from
  and what keeps the budget met.
- When the data source is versioned, cache by content revision, never by TTL. A TTL over
  versioned data either serves stale content after a republication or regenerates unchanged
  content on a timer.
- When a request's variants are a small declared set — preset windows, fixed slots —
  precompute them when the underlying data publishes rather than at request time.
- A response carries only what its surface renders. Never ship a payload the client filters
  down, and never fetch data a rendered state cannot display.
