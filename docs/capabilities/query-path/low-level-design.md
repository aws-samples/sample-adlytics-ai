# Query Path — low-level design

## 6. Interfaces and contracts

| Interface | Shape |
|---|---|
| Semantic planner | Declared operation request and semantics to a checked plan plus required scopes, or refusal. |
| Publication resolver | Required scopes and interval to complete successful references and exact coverage, or refusal. |
| Query port | Checked plan, selected references, and bound values to typed rows. |
| Target operation executor | Checked declared operation and attested snapshot to typed structures and derivation binding. |
| Figure shaping | Rows, plan, and selected evidence to typed values, states, semantic roles, and qualifiers. |

## 7. Concepts applied

- [Provenance Chain](../../concepts/provenance-chain.md)
- [Canonical Model](../../canonical-model.md)
- [Conversational Runtime](../conversational-runtime/README.md)

## 8. Decisions

- [ADR-024](../../decisions/adr-024-plans-account-for-every-grain-column.md)
- [ADR-027](../../decisions/adr-027-a-tool-may-default-a-lens-never-a-population.md)
- [ADR-028](../../decisions/adr-028-a-validated-plan-replaces-the-template-registry.md)
- [ADR-048](../../decisions/adr-048-make-metric-compatibility-and-axis-aware-aggregation-runtime-semantics.md)
- [ADR-100](../../decisions/adr-100-serve-periodic-metrics-only-for-complete-measured-periods.md): a periodic metric plans only at one exact complete period, selected by `period_start` and `period_end`; a daily-additive operand sums that period's days; a figure combining two shopper-aggregate deliveries is refused (`src/domain/measurement/periodic.py`)
- [ADR-106](../../decisions/adr-106-keep-outcome-metrics-inside-one-network-by-type.md)

## 9. Risks and open questions

- Missing/invalid required `CURRENT` must refuse before Athena.
- Exact holes remain explicit; no operation widens them.
- Target atomic operations require authoritative runtime identity, evidence binding, registration, and returned acceptance before they can be active.
- Deployed acceptance conclusions belong to [validation results](../../../validation/RESULTS.md).
