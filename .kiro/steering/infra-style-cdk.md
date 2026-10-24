---
inclusion: fileMatch
fileMatchPattern: ["**/infra/bin/**/*.ts", "**/infra/lib/**/*.ts", "**/infra/test/**/*.ts", "**/infra/cdk.json", "**/infra/package.json", "**/infra/tsconfig.json"]
description: Governs CDK versioning, typed infrastructure configuration, cross-cutting mixins, construct-library inspection, and stable construct-level selection when editing infrastructure code or configuration.
---

# CDK rules

Layout and rationale: docs/repo-structure.md §3. Module stability moves, so
re-check a construct's level before adopting it rather than trusting this file.

Cross-stack values are owned by infra-composition-stacks. Function definition,
runtime, packaging and sizing are owned by infra-compute-lambda. Neither is
restated here.

## Versioning and config

- CDK v2 only. aws-cdk-lib and the CLI are versioned independently; never pin
  them to the same string.
- Use the typed config module in infra/lib/config for per-environment values.
  Never use cdk.json context: a typo in a context key is a runtime undefined,
  while a typo in a typed config object is a build error.
- If synth produces cdk.context.json, commit it; it is a cache, and committing it
  makes synth reproducible.
- Put cross-cutting concerns in infra/lib/mixins: tagging, encryption, log
  retention, removal policy. Never set them per construct.

## Configuration values

- Never give an environment-derived URL, hostname, pool ID or client ID a
  fallback value. Absent must fail synth. infra/test/config.test.ts is the gate.

## Reading the construct library

- Read the .d.ts when you need to know what a construct accepts or renders.
  Never grep the compiled .js: aws-cdk-lib ships it minified onto single lines,
  so one match returns the whole module.
- Verify what an L2 passes to its L1 by reading the construction verbatim, not
  by extracting property names with a pattern. A regex that assumes an indent
  returns zero keys against minified source, and zero keys reads as "the L2 does
  not set it" — the same answer a real absence gives.

## Module stability

- Never add an @aws-cdk alpha package. Use the aws-cdk-lib module for
  apigatewayv2 and cognito-identitypool; for glue and s3tables an alpha exists and
  ADR-016 declines it anyway. An alpha's deprecation status is not the reason —
  the reason is that an alpha's API may break on any minor bump (ADR-005 records
  why aws-lambda-python-alpha is additionally rejected).
- Write Cfn resources and explicit IAM for every module make check-construct-levels
  reports as L1 only. Run it rather than working from a list here — the list moves,
  and a failure means a stable L2 landed and ADR-016 reopens for that service.
  Per-service evidence: docs/decisions/adr-016-data-layer-construct-levels.md.


