# Load Path — low-level design

## 6. Interfaces and contracts

| Interface | Responsibility |
|---|---|
| Finalized landing | Exact immutable canonical object and manifest-last completion. |
| Source pack | Interpretation, identity, row time, basis, mapping, and candidate rules. |
| Immutable storage | Create source-aligned, candidate, and successful bytes; accept only byte-identical replay. |
| Publication state | Append attempts, acquire fenced leases, create success records, and move `CURRENT`. |
| Validation gate | Typed source, suppression, semantic, DQ, and control evidence. |

### Pack-versioned columns

A governed query pack can add an output column at a later version. The contract field mapping
declares `present_from_pack_version: N` (N ≥ 2) on it, and the column must be nullable with
`not_applicable_under_options`, because a delivery an earlier pack version produced doesn't
carry it: the pack version is the option. The generated DQ ruleset therefore doesn't require
the column; it can't see the delivering pack.

| Mechanism | Rule |
|---|---|
| Load assertion `pack_version_column` | A delivery whose manifest governance names version N or later and leaves the column NULL on any row is blocked. The whole delivery is refused, not only the column, so every figure for its days waits on a corrected re-land. It rests on the pack's `SUM` returning a value on every kept row [unverified: no live AMC run of the new version yet; the owning slice's live acceptance checks it]. An earlier version's delivery passes with the column NULL |
| Publication evidence `contract.<contract_id>.query-pack-version.v1` | Every partition of a contract with a pack-versioned column records the delivering `query_pack_id` and `query_pack_version` (family `contract_assertions`). So does every partition of a contract with a `served_where_published` field (non-additive only; the contract must bound a grain column), which serving reads as covered from version 1; the serving rule is in the [Canonical Model](../../canonical-model.md) `fct_ad_exposure` section |
| Serving coverage | The planner reads that evidence through `PackCoveragePort` to decide which days a metric over the column covers; the planning rule is in the [Canonical Model](../../canonical-model.md) metric section |

The earlier version's deliveries load only while its exact binding stays in the pack's
`retainedVersions` ([Source Acquisition, governed pack](../source-acquisition/low-level-design.md#6-interfaces-and-contracts)).
The first use is `purchases_new_to_brand_incl_halo_12mo` on `amc-attributed-conversion` version 2
([Canonical Model](../../canonical-model.md)).

## 7. Concepts applied

- [Data-Flow Completion and Service Levels](../../concepts/data-flow-completion-and-service-levels.md)
- [Provenance Chain](../../concepts/provenance-chain.md)
- [Slowly Changing Dimensions](../../concepts/slowly-changing-dimensions.md)

## 8. Decisions

- [ADR-030](../../decisions/adr-030-the-raw-zone-landing-contract.md)
- [ADR-043](../../decisions/adr-043-a-landed-manifest-is-the-trigger-and-serialisation-is-the-correctness-requirement.md)
- [ADR-044](../../decisions/adr-044-materialize-source-aligned-data-and-centralize-canonical-publication.md)
- [ADR-047](../../decisions/adr-047-separate-attempts-leases-publications-and-coverage.md)
- [ADR-074](../../decisions/adr-074-define-authority-backed-data-flow-completion-and-service-level-objectives.md)
- [ADR-075](../../decisions/adr-075-generate-page-artifacts-once-per-dependency-cohort.md)

## 9. Risks and open questions

- Source-specific multipart behavior must be explicit in a governed pack.
- Concurrent deliveries to one physical Iceberg table can collide on commit until the
  [write discipline](write-discipline.md) mandated by ADR-075 is implemented; raising concurrency is
  not an accepted remedy.
- Immutable objects unreachable after a failed pointer transition are operational cleanup, not serving authority.
- Sanitized acceptance conclusions remain in [validation results](../../../validation/RESULTS.md).
