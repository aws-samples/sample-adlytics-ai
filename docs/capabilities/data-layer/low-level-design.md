# Data Layer — low-level design

## 6. Interfaces and contracts

| Interface | Responsibility |
|---|---|
| Finalized landing | Exact canonical object versions and manifest-last completion. |
| Source pack | Source interpretation, identity, row time, basis, mapping, and candidate rules. |
| Generated V2 declarations | Storage and catalog shape consumed by Python and CDK. |
| Physical compatibility manifest | Stable persisted field order/type/nullability; additive physical changes only. |
| Publication storage | Immutable source, candidate, success, and result objects. |

### Delivered column names

A source-pack field mapping fixes the header the delivered file carries for that field. Only a plain
source column is read off the source; every other kind is named by a convention the extraction must
honour, because the delivered file cannot say otherwise.

| Mapping | Delivered as |
|---|---|
| A plain source column | the last segment of its `source_path` |
| A derived value | its `target_column`, because the extraction aliases it to the canonical name |
| An extraction-option column (`from_extraction_option`) | its `target_column`, because the extraction chooses the option's name |
| A literal or load-assigned column | never delivered; produced during mapping or load |

Aliasing to the canonical name is a constraint on every extraction the repository writes, not an
observation about a source: any other alias would need a second mapping table to undo it. The
header assertion therefore checks delivered names, and the fixture generator writes the same names.

## 7. Concepts applied

- [Provenance Chain](../../concepts/provenance-chain.md)
- [Canonical Model](../../canonical-model.md)
- [Slowly Changing Dimensions](../../concepts/slowly-changing-dimensions.md)
- [Publication State](../publication-state/README.md)

## 8. Decisions

- [ADR-002](../../decisions/adr-002-canonical-data-model.md)
- [ADR-003](../../decisions/adr-003-canonical-model-decomposition.md)
- [ADR-044](../../decisions/adr-044-materialize-source-aligned-data-and-centralize-canonical-publication.md)
- [ADR-045](../../decisions/adr-045-separate-delivery-commerce-marketplace-and-product-identities.md)

## 9. Risks and open questions

- Source facts must be settled in the owning [source references](../../sources/) before a pack widens acceptance.
- Physical compatibility preserves persisted Iceberg identity; breaking storage shapes require a new versioned migration.
- Acceptance conclusions are owned by [validation results](../../../validation/RESULTS.md), not this design.
