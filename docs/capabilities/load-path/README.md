# Load Path

| | |
|---|---|
| **Level** | L2 — capability overview |
| **Status** | Semantic V2 publication contract. |
| **Owns** | Ordered ingestion from manifest arrival through immutable success and fenced `CURRENT` transition. |
| **Conventions** | [Documentation conventions](../../conventions.md) |

> **Revision proposed, 2026-10-07.** [ADR-089](../../decisions/adr-089-onboard-api-sources-through-governed-operation-packs-and-resolve-campaigns-at-read-time.md) (Proposed) would add two JSON-records reader formats for API sources and have every new contract declare the tables it writes. See [Response curves and budget simulation](../../concepts/response-curves-and-budget-simulation.md). This package changes when it is accepted and built.

## 1. Purpose and scope

Load Path turns one landed manifest into source-aligned evidence, canonical candidate evidence, and an immutable successful publication—or refuses without changing the last good `CURRENT`.

It does not own producer landing, physical data shapes, selector policy, or query computation.

## 2. Where this fits

This capability refines the V2 publication pipeline in the L1 [architecture](../../architecture.md#capability-index) and layers 2–6 of the [Provenance Chain](../../concepts/provenance-chain.md).

## 3. Black-box contract

| | |
|---|---|
| **Inbound** | Final manifest binding exact immutable source VersionIds. |
| **Outbound** | Immutable source-aligned, candidate, and successful publications; append-only attempts; fenced authority transition. |
| **Guarantee** | Source, identity, suppression, semantic, DQ, control, dependency, or fencing failure cannot create authority or extend coverage. |

## Documentation map

- [Architecture](architecture.md) owns the ingestion components and publication flow.
- [Low-level design](low-level-design.md) owns interfaces, concepts, decisions, and risks.
- [Publication State](../publication-state/README.md) owns the state records and serving policy.
- [Data-Flow Completion and Service Levels](../../concepts/data-flow-completion-and-service-levels.md) owns manifest-to-semantic clocks and completion evaluation.
