---
inclusion: fileMatch
fileMatchPattern: ["contracts/amc/*.yaml", "contracts/vendor-central/*.yaml"]
description: Governs source-dataset-to-curated-table YAML definitions, including scope, grain, field mapping, null, currency, and versioning rules, when editing AMC or Vendor Central data contracts.
---

# Data contract rules

Target model: docs/canonical-model.md. Load semantics: docs/capabilities/data-layer/README.md.
Source-specific rules live in data-contract-amc and data-contract-vendor.

## Scope of one contract

One contract covers one source dataset at one grain producing one curated table.
A source document containing two grains is two contracts, never one.

Every contract declares all of:

- source system, source dataset, and the exact source path of every field
- target curated table and its full column list
- grain, stated as the column list one row is unique on
- partition key
- measurement_basis as a literal value
- source timezone
- for every monetary column, the scale factor applied
- for every nullable column, which meaning of null applies

## Prohibitions

- Never omit measurement_basis. It is stamped as a literal during mapping and
  never read from a source, because no source knows it is one of several.
- Never declare a scale factor per table or per platform. It is per column.
- Never collapse the meanings of null. Suppressed, not-applicable-under-options,
  not-yet-available and unsupported-by-channel are four different facts, and a
  status column must say which.
- Never store a bare monetary amount. Currency travels with every money column.
- Never store a ratio. ROAS, CTR, CVR and CPC are derived in the semantic layer.
- Never encode grain in a table name. Every regrain would become a rename.
- Never use reject-on-unknown parsing. Both sources add fields without notice,
  so schema-on-read must be additive-tolerant.
- Never let a contract assert a fact marked unverified in its source doc. If
  docs/sources says the fact is unconfirmed, the contract cannot depend on it.
- Never assume conformance happened upstream. We do not run extraction and cannot
  know which query version produced a file, so validate and conform every load.
  Upstream is an untrusted producer and the manifest is the only statement of
  what actually arrived.

## Change

- Additive changes only without a version bump. A removed or retyped column is a
  new version.
- A contract is a declarative YAML file validated against a schema by a make target.
  make validate-contracts is the gate.
