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

The deployed development path materializes each source-aligned, candidate, and successful projection
one after another; a projection whose readback does not already match runs a read, one or more batched
merges, and a verifying re-read, with result reuse disabled. Its lease protects a partition while every
partition mapping to one physical Iceberg table — canonical tables keyed per dataset, source-aligned
tables keyed per interpreter — commits to that table.
[ADR-075](../../decisions/adr-075-generate-page-artifacts-once-per-dependency-cohort.md) mandates the
replacement: combine compatible immutable-record merges for one table into the fewest statements Athena
permits, verify once per table write unit, execute independent projections through the configured bounded
concurrency with position-preserving joins, and give each physical Iceberg table one writer at a time
through a FIFO message group per table with a per-table lease as the commit fence. Authority — immutable
bytes, attempts, publications, and `CURRENT` — stays in the delivery invocation under its live partition
lease; only the Iceberg projection moves, and a servable projection pointer on `CURRENT` tells readers
when it has landed. The [write discipline](write-discipline.md) companion owns the exact mechanism. Timeout and concurrency
ceilings remain execution limits, never remedies.
