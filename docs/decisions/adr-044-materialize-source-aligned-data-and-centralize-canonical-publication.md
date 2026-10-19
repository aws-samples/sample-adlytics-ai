# ADR-044: Materialize source-aligned data and centralize canonical publication

Date: 2026-08-24
Status: Accepted and implemented locally; deployment acceptance pending

> **Implementation amendment, 2026-08-24.** Source-aligned materialization and the single-owner
> canonical publication boundary are implemented locally as the Semantic V2 target. The adoption-time
> migration language below is retained as history; current implementation is owned by
> [data-layer](../capabilities/data-layer/README.md), [load-path](../capabilities/load-path/README.md) and
> [Publication State](../capabilities/publication-state/README.md). Deployment, live-publication, cutover, and
> V1-retirement acceptance remain pending in
> [validation results](../../validation/RESULTS.md#pending-acceptance--2026-08-24).

This established the Semantic Architecture V2 target before its source packs, query packs, loaders
and registries were delivered. Those statements below describe the adoption context, not current
implementation status.

## Context

At adoption, the path mapped a landed source document directly into canonical rows. That kept the
path short, but made two concerns move together: preserving what a source actually said and publishing
the business vocabulary every downstream query should use. It also permitted several contracts to
write one canonical fact, so source-specific delivery choices became shared-table write semantics.

Neither extreme is acceptable. Keeping only canonical rows loses the source-aligned evidence needed
to explain a mapping, restatement, or correction. Keeping only source-shaped rows makes every query
consumer reinterpret source tables, platform names, channel names, identifiers, and time semantics.
The existing [provenance chain](../concepts/provenance-chain.md) already requires source system,
dataset, measurement basis, coverage, and completeness to narrow monotonically; V2 needs a storage
boundary that preserves that evidence without exposing it as the business model.

The vocabulary distinction is load-bearing. A **source system** and **source dataset** identify where
evidence came from. A **platform** and **channel** describe the business activity being analysed.
They can correlate, but they are not aliases: one source can describe several channels, and several
sources can describe one platform or channel.

## Decision

**Materialize one source-aligned layer between landed objects and canonical facts, and give every
canonical dataset exactly one publishing owner.**

The source-aligned layer:

- preserves source-system and source-dataset provenance, delivery identity, source identifiers,
  source time semantics, source option set, and the mapping version used;
- is typed and queryable for validation and replay, but remains source-aligned rather than pretending
  to be the shared business model;
- may normalize physical representation such as parsing, types, and explicit nulls, but may not erase
  or relabel source meaning; and
- is not a second raw zone: the landed object remains the immutable evidence, while the materialized
  layer records the interpreted source rows that a canonical publisher consumed.

The canonical layer:

- uses **platform** and **channel** as business vocabulary; source system and dataset remain lineage,
  not substitute analytical dimensions;
- is published only by the named owner for that canonical dataset;
- accepts any number of source-aligned inputs through that owner, which alone resolves mappings,
  compatibility, precedence, and canonical partition identity; and
- carries a reversible reference to the source-aligned publications that contributed to each
  canonical publication.

A contract may own source interpretation. It may not become a second writer to a shared canonical
fact. Adding a source therefore adds or changes an input to the canonical owner; it does not grant a
new component permission to mutate the canonical dataset.

The field-level canonical vocabulary and source crosswalk remain owned by
[canonical-model](../canonical-model.md). Landing and per-layer provenance obligations remain owned by
[provenance-chain](../concepts/provenance-chain.md) and [load-path](../capabilities/load-path/README.md).

## Consequences

**Source fidelity and business consistency stop competing.** A source-specific discrepancy can be
inspected before canonicalization, while all analytical consumers see one platform/channel
vocabulary.

**Canonical publication has one place to enforce semantics.** Cross-source precedence, identity
resolution, time class, metric compatibility, and publication validation cannot vary by source
adapter or contract.

**Shared canonical facts are single-writer stores in the local V2 implementation.** Paths that let
several contracts write one fact are migration inputs, not the ownership model. Deployment,
live-publication, cutover, and V1-retirement acceptance remain pending in
[validation results](../../validation/RESULTS.md#pending-acceptance--2026-08-24).

**Storage and latency increase.** A successful delivery produces a source-aligned publication before
a canonical publication. The extra boundary is accepted because replayability and single-owner
publication are correctness properties rather than caching conveniences.

**Platform/channel mappings become governed semantic changes.** A source label cannot silently
become a new business channel. The canonical owner must map it explicitly, and an unresolved mapping
blocks publication rather than leaking source vocabulary into the analytical surface.

## Alternatives rejected

| Rejected | Why |
|---|---|
| Map landed objects directly into canonical facts | Couples source interpretation to shared-table mutation and leaves no materialized source-aligned evidence for replay or diagnosis |
| Keep source-shaped tables and canonicalize only in query views | Makes every query path a canonical publisher, so mappings and compatibility can diverge by consumer |
| Let every source contract publish its canonical rows | Recreates multi-writer ownership; precedence and cross-source compatibility become conventions rather than one enforced boundary |
| Rename source systems to platforms and datasets to channels | Erases provenance and makes the business vocabulary depend on physical acquisition choices |

## Review trigger

Revisit if a canonical dataset has a legitimate need for independent publishers that cannot be
expressed as inputs to one owner. The review must identify how conflicting publications, provenance,
and rollback remain deterministic; implementation convenience is not sufficient.
