# Capability designs

Each package is one composite L2 capability document: `README.md` owns sections 1–3, `architecture.md` owns sections 4–5, and `low-level-design.md` owns sections 6–9 of the [capability template](../conventions.md#capability--capabilitymd). A companion document belongs inside its parent package when it has a bounded internal interface but is not independently deployable.

| Capability | Responsibility | Current status |
|---|---|---|
| [Source Acquisition](source-acquisition/README.md) | Governed producer packs, registration, and manifest finalization. | Optional; disabled until explicit onboarding inputs exist. |
| [Data Layer](data-layer/README.md) | Canonical semantics and immutable Semantic V2 storage shapes. | Current data contract. |
| [Load Path](load-path/README.md) | Ordered immutable publication and fenced authority transition. | Current publication contract. |
| [Publication State](publication-state/README.md) | Attempts, leases, immutable successes, `CURRENT`, and exact coverage. | Current serving-authority contract. |
| [Query Path](query-path/README.md) | Exact selection, governed computation, and typed figures/refusals. | Current deterministic computation contract. |
| [Conversational Runtime](conversational-runtime/README.md) | Verified turns, managed Harness invocation, the streamed agent answer, and memory. | Current repository path; `ConversationRequest.v3` and `ConversationStream.v4` own its public wire. |
| [Analytics Discovery](analytics-discovery/README.md) | Declared navigation from business questions to existing analytical pages. | Proposed. |
| [Forecasting](forecasting/README.md) | Offline baseline forecast artifacts. | Proposed; review is required before implementation. |
| [Saturation](saturation/README.md) | Offline response-curve fitting and bounded saturation artifacts. | Proposed; no implementation or acceptance evidence. |
| [Agent Quality Loop](agent-quality-loop/README.md) | Content-minimized audit, structural findings, and protected linkage. | Current repository quality boundary; dated acceptance scope is owned by validation results. |
| [Protected Quality Review API](protected-quality-review-api/README.md) | Entitled review of one turn and a bounded turn listing, for cookie and bearer callers. | Implemented and dev accepted (2026-09-28). |
| [Operational Data-Flow Status](operational-data-flow-status/README.md) | Complete content-free lifecycle projection and protected operator status API. | Accepted by ADR-073; observation-only on dev, live since 2026-09-19; evaluated SLO activation gated by ADR-074. |

[Architecture](../architecture.md#capability-index) is the system-wide black-box index. [Validation results](../../validation/RESULTS.md) own acceptance conclusions; capability packages describe current contracts rather than execution history.
