# Load Path — architecture

## 4. Internal structure

The V2 ingestion path verifies a manifest and exact source bytes, parses once, materializes source-aligned evidence and a canonical candidate, evaluates publication gates, writes immutable successful bytes, then advances authority through one fenced state transaction.

## 5. Key runtime flows

1. Read the manifest and every source object by VersionId; verify identity, byte count, and checksum.
2. Append a started attempt and acquire a scope- or sibling-scoped fencing lease.
3. Materialize source-aligned and canonical candidate evidence from one parse.
4. Evaluate source, suppression, semantic, DQ, and control gates.
5. On success, create immutable successful bytes before advancing `CURRENT`; on failure, append a typed terminal attempt while preserving prior authority.
6. Governed sibling scopes succeed and advance together or remain unchanged together.

## 5a. Write discipline

Each table write unit commits with two Athena statements at any admitted size
([ADR-149](../../decisions/adr-149-write-each-table-unit-with-one-set-based-merge-from-a-staged-object.md)).
The writer stages the unit's exact records once, as a content-addressed object in the staging bucket,
then runs one insert-only `MERGE` from that object's generated staging table and one set-based
verification that compares four counts per identity inside Athena and returns no rows. Result reuse
stays disabled. A unit over 50,000 rows or a delivery over 20,000 is refused before authority; those
ceilings come from the writers' memory and 900 s run. Its lease protects a partition while every
partition mapping to one physical Iceberg table — canonical tables keyed per dataset, source-aligned
tables keyed per interpreter — commits to that table.
[ADR-075](../../decisions/adr-075-generate-page-artifacts-once-per-dependency-cohort.md), as amended by
ADR-149, sets the rest: verify once per table write unit, execute independent projections through the configured bounded
concurrency with position-preserving joins, and give each physical Iceberg table one writer at a time
through a FIFO message group per table with a per-table lease as the commit fence. Authority — immutable
bytes, attempts, publications, and `CURRENT` — stays in the delivery invocation under its live partition
lease; only the Iceberg projection moves, and a servable projection pointer on `CURRENT` tells readers
when it has landed. The [write discipline](write-discipline.md) companion owns the exact mechanism. Timeout and concurrency
ceilings remain execution limits, never remedies.
