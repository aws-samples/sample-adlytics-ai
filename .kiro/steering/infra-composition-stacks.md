---
inclusion: fileMatch
fileMatchPattern: ["**/infra/lib/stacks/**/*.ts"]
description: Governs CDK stack boundaries, resource lifecycle separation, SSM-based cross-stack value exchange, deployment-order ownership, and stack naming when editing infrastructure stacks.
---

# Stack composition

Decision and rationale: ADR-012. General CDK conventions: infra-style-cdk.
Which stacks are RETAIN is visible in each stack's removal policy and asserted by
its test; it is not listed here.

## Where the boundary falls

Cut by what may be destroyed together first, rate of change second, logical unit
third. Apply them in that order — a resource in the right logical unit and the
wrong blast radius is in the wrong stack.

- Never put a RETAIN resource in the same stack as a DESTROY resource. A stack is
  only as safe as its most dangerous member, and this boundary is what protects
  customer data.
- A new DynamoDB table declares managed encryption, on-demand billing, PITR, and
  its retention/removal policy explicitly in its owning stack; never rely on a
  service default for any of the four.
- Never let a resource that holds conversation or customer data be created by a
  service as a side effect of another resource. A memory the harness manages, a
  user pool an authorizer default creates: both are deleted with their parent and
  neither appears in a template to be protected. Create it yourself and pass its
  ARN.
- A managed AgentCore telemetry or evaluation log group is the only exception when
  the service API offers no customer-supplied destination. The owning feature MUST
  deploy disabled first, identify the exact generated group, apply and verify the
  retention configured in `infra/lib/config`, and only then enable content writes.
  If the group identity changes, repeat that gate before enabling. Scope service
  and reviewer IAM to that group, prohibit public export, and record service-owned
  encryption and lifecycle limits in `docs/concepts/observability.md`; if any pre-enable
  control cannot be applied, do not enable the feature.
- Never split a stack to escape a circular dependency. A cycle means the boundary
  is wrong, and SSM removes the mechanism rather than the design error.

## Exchanging values

- Every value one stack needs from another is an SSM parameter. Never a
  construct reference across a stack boundary, never Fn::ImportValue.
- Never type a parameter path as a literal at a call site. Add a key to PARAMS in
  lib/config so a rename is a compile error.
- Read with valueForStringParameter, which resolves at deploy. Never
  valueFromLookup for a value this app produces.
- A value produced in another region is still an SSM parameter, read at deploy by
  a cross-region parameter read. Never a construct reference or an export.
- Grant ssm:GetParameter on the specific path. Never a wildcard over the
  parameter prefix PARAMS owns.
- Never rename a parameter path in one deploy. Add the new path, deploy every
  consumer, then remove the old one.

## What SSM does not give us

- DEPLOY_ORDER in infra/lib/config/index.ts is the executable statement of the
  deploy sequence. The README's walkthrough derives from it and names it as the
  authority; never order stacks in code, tests, or scripts from any other source.
- After changing a parameter's value, redeploy every stack that reads it. Nothing
  does this automatically.

## Naming

- Never put an account ID or region in a stack ID. Those come from the
  environment the stack is deployed into.
