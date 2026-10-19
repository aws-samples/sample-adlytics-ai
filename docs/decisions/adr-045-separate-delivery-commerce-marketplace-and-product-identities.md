# ADR-045: Separate delivery, commerce, marketplace, and product identities

Date: 2026-08-24
Status: Accepted and implemented locally; deployment acceptance pending

> **Implementation amendment, 2026-08-24.** Effective-dated aliases and product namespace rules
> implement this decision on the local Semantic V2 target path. The adoption-time migration
> consequences below remain historical. Current resolver behaviour is owned by
> [data-layer](../capabilities/data-layer/README.md). Deployment, live-publication, cutover, and V1-retirement acceptance
> remain pending in [validation results](../../validation/RESULTS.md#pending-acceptance--2026-08-24).
> This implementation does not create the global cross-retailer identity that the decision explicitly
> declines to claim.

This established the Semantic Architecture V2 identity target before source-pack and query-pack
implementation.

## Context

A landed delivery has identifiers needed to fetch, deduplicate, order, and replay bytes. Analytical
facts have identifiers needed to state whose commerce activity is measured, in which marketplace,
and for which product. Some current values look interchangeable because the same source string is
available at more than one boundary. They are not interchangeable.

The failure mode is plausible data attached to the wrong analytical population. A marketplace token
used in an object prefix may be a producer's routing value; a marketplace in a fact is a business
identity with join, currency, and population semantics. A product code may be unique only inside one
marketplace. An advertiser, vendor, merchant, account, and customer deployment may all describe a
commerce scope without being the same identifier. Reusing one string across these roles bypasses the
mapping where disagreement should be detected.

[ADR-030](adr-030-the-raw-zone-landing-contract.md) owns physical delivery layout.
[canonical-model](../canonical-model.md) owns analytical dimensions and joins. Source-specific
identifier evidence belongs in [sources/amc](../sources/amc.md) and
[sources/vendor-central](../sources/vendor-central.md).

## Decision

**Delivery identity, commerce identity, marketplace identity, and product identity are separate
typed domains with explicit resolution between them.**

- **Delivery identity** identifies physical evidence and its execution context: source system,
  source dataset, option set, extraction interval, retrieval, object and checksum. It supports
  idempotency, ordering, provenance, and replay. It is never an analytical join key.
- **Commerce identity** identifies the business scope whose activity is measured. Source account,
  advertiser, vendor, merchant, brand, and customer deployment identifiers remain qualified by
  their namespace until an explicit relationship resolves them. Physical tenancy alone does not
  establish analytical equivalence.
- **Marketplace identity** identifies the canonical market population. Producer routing values,
  marketplace names, marketplace IDs, country, region, and currency are attributes or source keys,
  not aliases. Resolution produces one canonical marketplace identity or refuses publication.
- **Product identity** identifies a product within its declared namespace and scope. A source-local
  product code is not promoted to a universal product identity. Marketplace-qualified Amazon
  identity and any future cross-retailer identity remain distinct unless a governed crosswalk proves
  the relationship.

Each identifier carries its namespace in its type or value object. Conversion from one domain to
another occurs only through a named resolver that records the source key, canonical key, mapping
version, and resolution outcome. String equality is never a resolver.

A canonical publication contains analytical identities and references its delivery provenance. The
delivery provenance may explain where a row came from, but it cannot fill a missing commerce,
marketplace, or product identity. An unresolved or ambiguous required identity fails semantic
validation before publication.

## Consequences

**Physical retries and analytical restatements become independent questions.** Exact bytes can be
recognized without treating the object key as the identity of the commerce population they describe.

**Cross-source joins become explicit and auditable.** Two rows join because their canonical
marketplace, commerce scope, or product identity resolved compatibly, not because source strings
happened to match.

**Some adoption-time convenient mappings became refusals.** A producer-provided marketplace segment
or product code could not be stamped directly into a canonical fact unless the relevant resolver
accepted it. That increased quarantine or rejection during migration and prevented silent population
drift; the implemented V2-only path now enforces the resolver boundary.

**Future global product identity remains possible without pretending it exists.** The current model
may retain marketplace-qualified product identity while a later cross-retailer crosswalk introduces
a different type. This ADR does not claim such a crosswalk is available.

**More lineage is retained.** Canonical rows or publication metadata must preserve the source keys and
mapping version required to reproduce each resolution.

## Alternatives rejected

| Rejected | Why |
|---|---|
| Use one generic string identifier everywhere | Makes invalid joins type-correct and turns namespace mistakes into plausible analytical results |
| Treat the manifest marketplace as the canonical marketplace | Confuses producer routing with business identity and bypasses marketplace resolution |
| Treat an ASIN as globally unique without marketplace scope | Makes a source-local commerce identifier claim more universality than its evidence supports |
| Infer commerce identity from the deployment or credential | Physical access scope does not prove advertiser, vendor, merchant, or brand equivalence |

## Review trigger

Revisit when a verified identifier can safely collapse two domains for every supported source and
marketplace. The evidence must establish semantic equivalence, not merely matching strings in the
current datasets.
