# ADR-029: The curated zone is Apache Iceberg, not plain Parquet

Date: 2026-08-19
Status: Accepted — physical schema amendment 2026-08-24

> **Physical schema amendment, 2026-08-24.** Iceberg identifies columns by field ID, but Glue
> ignores submitted arbitrary IDs and persists one-based ordinals in submission order. Therefore
> physical evolution in this implementation is append-only: a governed compatibility manifest owns
> every existing canonical column's order, type, and physical nullability; generation rejects a
> removal, rename, retype, tightening, or reorder; and new columns receive IDs above the historical
> maximum. New fields on existing tables are physically nullable until retained evidence is
> reprocessed, while logical contracts and publication DQ continue requiring them on every new row.

Records `D-01`, which has shaped the data layer from the beginning and until now had its rationale only in [data-layer](../capabilities/data-layer/README.md) and [finding register](../decision-coverage-registry.md#finding-register-f-nn) — a design doc and a findings log, neither of which owns a decision. Nothing about the choice changes here. It is written down late because [architecture](../architecture.md) §11 flagged it as the one entry in the decision index with no record, and an undocumented decision is one that gets revisited by accident.

## Context

The curated zone holds conformed analytical tables on S3. The default answer is partitioned Parquet with a Glue table over it, which is cheap, universally readable, and what most lakes are.

Three properties of *these* sources make that default fail, and all three were known before any table was built.

**Both sources restate history.** Vendor Central rewrites a window's figures as returns and cancellations arrive — 45 days for sales — and AMC's attribution tables legitimately return different numbers on re-execution. So a load is not an append. It is a replacement of a window that may already have been replaced, and correctness depends on being able to overwrite a partition without a reader ever seeing it half-written.

**A dimension has to keep history.** `dim_product` records what a product's brand *was*, which means a load closes some rows and opens others in the same operation. With plain Parquet the only mechanisms are rewriting a whole partition or maintaining a side index of which files are current — the first is expensive and the second is a second source of truth about the same rows.

**Amazon adds fields to reports without notice**, which their own documentation says plainly. So the schema changes on their schedule, not ours, and a column added to a table cannot invalidate the files written before it.

The mechanics a plain-Parquet lake would need for those three, stated concretely: manual partition registration, a directory-swap protocol for atomic overwrite, positional column ordering that breaks the moment a contract inserts a column, and a hand-rolled convention for which files a reader should ignore. Each is a well-known pattern and each is a piece of correctness machinery we would own and test ourselves.

## Decision

**Curated tables are Apache Iceberg, in the Glue Data Catalog, written and read through Athena.**

Four capabilities are load-bearing and are the reason for the choice:

- **Snapshot isolation.** A reader sees the table as of a committed snapshot, so a partition overwrite is never observed half-applied. That is what makes a restatement safe to apply while a tool is querying.
- **Row-level `UPDATE` and `DELETE`.** SCD2 closure is a statement rather than a partition rewrite ([slowly-changing-dimensions](../concepts/slowly-changing-dimensions.md)).
- **Schema evolution by stable physical field ID.** Glue assigns IDs by ordinal, so generated
  physical schemas preserve the committed historical order and append new columns only. Logical
  contract order may change without changing existing physical identity; removing, renaming,
  retyping, or tightening a physical column requires an explicit migration.
- **Time travel.** A wrong load is readable back and rollable back, for as long as its snapshots are retained.

## Consequences

**Correctness machinery we would otherwise write is the table format's job**, under test by somebody else, and the three properties in Context are handled by one mechanism rather than three conventions.

**An `UPDATE` is copy-on-write, and the cost lands on reads.** This is the sharpest cost and it is measurable: [verified 2026-08-19] `dim_product` held 21 logical rows across **218 S3 objects** after its SCD2 loads. Compaction is therefore not optional — `CfnTableOptimizer` runs on all eight tables, Glue-managed rather than an `OPTIMIZE` issued by the load ([ADR-016](adr-016-data-layer-construct-levels.md)).

**Snapshots accumulate and nothing yet expires them.** Compaction merges the live data; it does not remove old snapshots, and orphan-file deletion is a separate optimizer. Storage therefore grows with load frequency rather than with data volume, and the retention window is an undecided question ([Q-17](../decision-coverage-registry.md#question-register-q-nn)) — deliberately, because the window that matters is how long it takes us to notice a wrong number, which nobody has stated.

**Renaming a canonical column is a migration, not an edit.**
`contracts/shared/iceberg-schema-compatibility.yaml` owns the physical identity already assigned to
canonical columns. The schema generator rejects a rename as removal of that identity. Adding a
column to an existing table appends a migration-nullable physical field; the contract remains strict
for new publications, and reprocessing fills the field before readers depending on it are deployed.

**Athena and Glue Iceberg constructs are L1 or alpha in CDK**, so the tables are written as `Cfn*` resources with explicit IAM ([ADR-016](adr-016-data-layer-construct-levels.md)). More infrastructure code than an L2 would be, and it does not break between minor versions.

**The engine is a constraint, not just a client.** Athena's Iceberg support is what defines the write vocabulary, and the boundary showed up in practice: one `MERGE INTO` cannot express an SCD2 change, and there is no transaction spanning two statements ([ADR-025](adr-025-applying-an-scd2-plan-without-a-transaction.md)).

## Alternatives rejected

| Rejected | Why |
|---|---|
| **Partitioned Parquet with a Glue table** | The default, and it needs a directory-swap protocol for atomic overwrite, manual partition registration, and a hand-rolled convention for which files are current. Three conventions to keep correct in place of one format that does it |
| **Delta Lake** | Comparable capabilities. Rejected on integration rather than merit: Athena and Glue treat Iceberg as first-class, so choosing Delta means paying in engine support for a feature set we do not need more of |
| **Hudi** | Same reasoning, and its strength is streaming upserts. These loads are batch windows arriving at most a few times a day |
| **A warehouse — Redshift or Snowflake** | Solves all three properties and more. Rejected because this repository is deployed into a customer's own account: a warehouse is a standing cost and an operational surface they did not ask for, where Athena bills per query and has nothing to run when nobody is asking |
| **S3 Tables** | Managed Iceberg, and genuinely attractive for the compaction and expiry this decision leaves us owning. Its CDK support is alpha and it is a newer service surface; revisiting it is the review trigger below rather than a rejection on substance |

## Review trigger

Revisit if compaction and snapshot management become recurring operational work rather than a setting — that is the case S3 Tables exists for, and the honest comparison needs the retention decision in [Q-17](../decision-coverage-registry.md#question-register-q-nn) made first, since it is the parameter both options are tuned by.
