# Publication State

| | |
|---|---|
| **Level** | L2 — capability overview |
| **Status** | Semantic V2 serving-authority contract. |
| **Owns** | Attempts, fenced leases, immutable successes, `CURRENT`, and exact coverage selected for reads. |
| **Conventions** | [Documentation conventions](../../conventions.md) |

## 1. Purpose and scope

Publication State separates operational evidence from serving authority. Attempts explain work, leases fence writers, immutable successes preserve validated publications, and `CURRENT` selects the only publications readers may use.

It does not own source mapping, publication sequencing, SQL composition, or response release.

## 2. Where this fits

This capability refines the Publication State container in the L1 [architecture](../../architecture.md#capability-index) and layers 5–6 of the [Provenance Chain](../../concepts/provenance-chain.md).

## 3. Black-box contract

| | |
|---|---|
| **Inbound writes** | Append attempts; acquire/renew fenced leases; create immutable successes; conditionally advance governed `CURRENT` sets. |
| **Inbound reads** | Inspect attempts and strongly resolve exact current successes for required scopes and buckets. |
| **Outbound** | Attempts, tokens, immutable success records, `CURRENT` selectors, and normalized coverage intervals/holes. |
| **Guarantees** | `CURRENT` is the sole serving selector; stale tokens cannot advance it; non-success cannot replace the last good version; holes remain visible. |

## Documentation map

- [Architecture](architecture.md) owns state topology and serving flows.
- [Low-level design](low-level-design.md) owns interfaces, decisions, and current risks.
- [Load Path](../load-path/README.md) owns ordered candidate construction and submission.
- [Data-Flow Completion and Service Levels](../../concepts/data-flow-completion-and-service-levels.md) owns required-child qualification and objective evaluation.
