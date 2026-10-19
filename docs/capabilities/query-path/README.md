# Query Path

| | |
|---|---|
| **Level** | L2 — capability overview |
| **Status** | Current deterministic computation contract. |
| **Owns** | Semantic planning, exact publication selection, governed SQL composition, Athena execution, and figure shaping. |
| **Conventions** | [Documentation conventions](../../conventions.md) |

## 1. Purpose and scope

Query Path turns a declared metric at a declared grain into deterministic figures, or refuses before Athena when semantics or current evidence are incomplete.

It does not own metric definitions, publication-state policy, landing, or conversational release.

## 2. Where this fits

This capability refines the Domain Tools and Query Engine containers in the L1 [architecture](../../architecture.md#capability-index) and layers 7–8 of the [Provenance Chain](../../concepts/provenance-chain.md).

## 3. Black-box contract

| | |
| **Responsibility** | Return figures from a complete exact successful current set, or refuse before SQL. |
| **Inbound** | Validated measurement request and governed semantic declarations. |
| **Outbound** | Typed figures, exact coverage, suppression evidence, mandatory qualifiers, and query diagnostics. |
| **Guarantee** | Selection precedes Athena; caller values are bound; publication IDs never become SQL text. |

## Documentation map

- [Architecture](architecture.md) owns the computation pipeline and operation boundaries.
- [Low-level design](low-level-design.md) owns interface shapes, concepts, decisions, and local risks.
- [Publication State](../publication-state/README.md) owns selector/coverage semantics.
