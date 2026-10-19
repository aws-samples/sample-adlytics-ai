---
inclusion: fileMatch
fileMatchPattern: ["contracts/vendor-central/**"]
description: Governs Vendor Central contract grain separation, report-option identity, null semantics, restatement monotonicity, source-time handling, and currency mapping when editing Vendor Central contracts.
---

# Vendor Central contract rules

Every fact below is owned by docs/sources/vendor-central.md, which carries its own
verification state. Check it before relying on one.

This repository does not call SP-API. An external producer lands report
documents in S3, so retrieval mechanics — rate limits, polling, presigned URLs,
compression, scheduling — are not our rules. What follows governs reading what
arrived.

## Two grains, two tables

- Split every non-real-time report into two curated tables: the aggregate array
  and the by-ASIN array. Never load them into one.
- Stamp all of reportSpecification onto every row of both tables: reportType,
  every reportOptions key, lastUpdatedDate, dataStartTime, dataEndTime,
  marketplaceIds. Without it a MANUFACTURING pull and a SOURCING pull are
  indistinguishable and double-count.
- Never expect marketplaceId on a row; project it down from reportSpecification,
  per docs/sources/vendor-central.md §4.
- Never map a traffic metric the source doc does not list;
  docs/sources/vendor-central.md owns the traffic report's roster.

## reportOptions is dataset identity

- Put reportOptions in the partition key and on every row. It is not a runtime
  flag, and the same reportType yields genuinely different effective schemas
  across pulls.
- Never assume a column is populated across reportOptions views. Which columns
  null under which distributorView or sellingProgram is owned by
  docs/sources/vendor-central.md; declare each nullability in the contract
  rather than discovering it at load time.
- Only distributorView MANUFACTURING reconciles to AMC. Model SOURCING as a
  separate, non-reconcilable fact table.
- Never write a distributorView or sellingProgram column into a traffic
  contract; the traffic report's options are per docs/sources/vendor-central.md §3.
- Never compute conversion rate outside MANUFACTURING. Traffic has no
  distributorView, so a SOURCING sales pull cannot be rate-ed against it.

## Restatement

- lastUpdatedDate is the restatement watermark. Map it to source_watermark,
  separate from the period bounds.
- Never append on a re-pull. The load key is PartitionKey in
  src/domain/curation/watermarks.py and the monotonicity decision is enforced
  there and again by a DynamoDB condition; ADR-047 and
  docs/capabilities/load-path/write-discipline.md own the design. Never restate
  the key's parts here.
- Never encode a fixed restatement horizon. No window is documented, and revenue
  is net of returns whose lag is unstated.
- Treat reportPeriod DAY as the source of truth and derive weeks and months
  ourselves. Weekly history cannot be backfilled; the lookback limits are per
  docs/sources/vendor-central.md §5.

## Source time semantics

- Read Vendor source-time labels, field semantics, configurability, and verification
  state from docs/sources/vendor-central.md; never restate those source facts in steering.
- Preserve each fact contract's declared source_timezone as opaque provenance on every
  curated fact row. Never translate, canonicalize, or infer an offset or IANA zone
  during date-grain curation.
- Treat each source date bucket as indivisible. Never rebucket it across time zones or
  reconstruct an instant or time of day from a date field; exact field semantics remain
  owned by docs/sources/vendor-central.md.
- Encode an offset or named-zone interpretation only after the source document records
  supporting evidence and ADR-004 records the corresponding design decision. Tests prove
  repository propagation and compatibility behavior; they never establish external DST
  behavior without a governed probe.

## Money and currency

- Money is a nested Amount object, never a scalar. Map one currency_code per
  fact, never one per field. Decided in ADR-021.
- Declare currency_agreement naming every source currencyCode path whose amount
  collapses onto that one column. make validate-contracts derives the expected
  set from the field mappings, so a short list fails.
- currencyCode is carried per value, not per report. No FX is provided.

## Prohibitions

- Never encode whether the vendor asin is a parent or a child. It is
  undocumented. Source the hierarchy from Data Kiosk parentAsin, per ADR-020.
- Never assume non-negative orderedUnits in the real-time sales report, per
  docs/sources/vendor-central.md §3.
- Never join vendor asin to AMC tracked_asin as advertised performance.
  tracked_asin includes same-brand products that were not advertised, biasing in
  the direction that flatters ROAS.
- Never join advertising to retail finer than ASIN by date by marketplace. There
  is no campaign dimension in any vendor report.
- Never map sellingProgram one-to-one to any AMC field. No such mapping is
  documented.
- Never target a report type the source doc records as removed;
  docs/sources/vendor-central.md owns that list.
- Never load the same dataset from both the Reports API and Data Kiosk. Facts
  come from the Reports API; only the asin-to-parentAsin hierarchy comes from
  Data Kiosk. Decided in ADR-020, which closes Q-05.
