---
inclusion: auto
name: amc-validation-workflow
description: Use for AMC source validation, query-pack execution or recovery, result interpretation, or changes to AMC validation probes, while excluding application runtime integration work.
---

# AMC validation workflow

AMC validates external source semantics only. The application runtime begins at the already landed source object; never add AMC credentials, clients, or grants to runtime code or infrastructure. See docs/sources/amc.md and validation/README.md.

Never use query differencing or broad rejected queries to infer a column-level conclusion. Probe selection and one-fact-per-probe rules are owned by validation-probes.

Before submission, re-discover the authorized target and recover matching server-side work from the local journal and filtered execution history. Never blindly resubmit work after a client interruption.

Classify Sandbox, validation-instance, and customer-volume evidence only within the limits stated by docs/sources/amc.md. Evidence custody and publication rules are owned by validation-probes; the import boundary is enforced by make lint-boundaries.
