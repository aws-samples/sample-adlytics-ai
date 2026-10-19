# Data Layer

| | |
|---|---|
| **Level** | L2 — capability overview |
| **Status** | Semantic V2 is the current analytical data contract. |
| **Owns** | Canonical semantics, source-pack interpretation, and immutable source-aligned, candidate, current, and result storage shapes. |
| **Conventions** | [Documentation conventions](../../conventions.md) |

> **Revision proposed, 2026-10-07.** [ADR-088](../../decisions/adr-088-model-budget-pacing-and-pool-state-as-platform-neutral-snapshots.md) (Proposed) would add a `snap_` storage kind beside `fct_` and `dim_`, never summed over its observation time, and new platform-neutral budget, pacing and pool state tables. See [Response curves and budget simulation](../../concepts/response-curves-and-budget-simulation.md). This package changes when it is accepted and built.

## 1. Purpose and scope

The Data Layer turns immutable manifest-bound source versions into declared source-aligned and canonical candidate data without losing source identity, grain, basis, units, options, or exact coverage.

Field-level dimensions and facts remain in the [Canonical Model](../../canonical-model.md). Load ordering belongs to [Load Path](../load-path/README.md); authoritative selectors belong to [Publication State](../publication-state/README.md); computation belongs to [Query Path](../query-path/README.md).

## 2. Where this fits

This capability refines the retained Semantic V2 source, semantic publication, and query-result containers in the L1 [architecture](../../architecture.md#capability-index).

## 3. Black-box contract

| | |
|---|---|
| **Responsibility** | Interpret exact source versions through governed packs into immutable source-aligned rows and canonical candidates. |
| **Inbound** | Verified canonical manifests, immutable source bytes, manifest facts, and governed source packs. |
| **Outbound** | Immutable source-aligned/canonical candidates and physical shapes consumed by publication and query capabilities. |
| **Guarantees** | Declared source meaning, retained basis/unit/grain, traceable source evidence, and no blocker represented as current. |
| **Not guaranteed** | Reconciliation between independently measured systems. |

## 4. Null meanings

Every nullable column declares why it can be null (`NullMeaning`, `src/domain/data_contracts/contract.py`). A null is never read as zero.

| Meaning | The value is absent because |
|---|---|
| `suppressed` | a privacy threshold withheld it |
| `not_applicable_under_options` | the source's options exclude it (vendor `SOURCING` or `FRESH`), or the contract's declared grain set doesn't split by that column ([ADR-099](../../decisions/adr-099-treat-distinct-shopper-counts-as-a-new-kind-of-fact.md) point 2) |
| `not_yet_available` | the period is partial and will be filled later |
| `unsupported_by_channel` | the channel has no such concept |
| `not_returned` | an expected cell of a declared closed enumeration came back with no row: too few shoppers, or none. A suppressed cell and an empty one look the same, so it claims neither. It renders "not available", and a sum across a disjoint axis that includes it is refused ([ADR-099](../../decisions/adr-099-treat-distinct-shopper-counts-as-a-new-kind-of-fact.md) point 8) |

## Documentation map

- [Architecture](architecture.md) owns physical-layer topology and semantic flows.
- [Low-level design](low-level-design.md) owns interfaces, compatibility, decisions, and local risks.
- [Canonical Model](../../canonical-model.md) owns fields and source crosswalks.
