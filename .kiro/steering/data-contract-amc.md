---
inclusion: fileMatch
fileMatchPattern: ["contracts/amc/**"]
description: Governs AMC contract source facts, suppression-safe mappings, governed query-pack parameterization, and unresolved source semantics when editing files under `contracts/amc/`.
---

# AMC contract rules

Before changing an AMC contract, read docs/sources/amc.md and the governed AMC
column registry, contracts/shared/amc-columns.yaml. The registry and make validate-contracts own field outputability,
scale, currency, threshold, and source-field facts; do not copy those facts into
steering or contract commentary.

- Use the declared contract suppression block and let the contract gate verify
  required extraction evidence. Do not map a per-delivery suppression diagnostic
  into a conformed target shared by multiple contracts.
- Use governed query-pack parameters. Never string-template AMC SQL.
- Never use cross-execution query differencing to infer a sensitive value.
- If the source reference or registry does not settle a required AMC fact, use
  amc-validation-workflow and update the source reference or registry before
  adding the field.
- A contract may use sensitive identifiers only inside a source expression needed
  to produce an allowed aggregate. Never map them into delivered output.
