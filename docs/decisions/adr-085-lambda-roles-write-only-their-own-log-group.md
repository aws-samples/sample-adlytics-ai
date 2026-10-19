# ADR-085: Lambda roles write only their own log group

Date: 2026-10-02
Status: Accepted 2026-10-02 (owner approval of the security solutions). Implemented in the repository on `fix/pre-publication`; dev acceptance recorded 2026-10-02 in [validation results](../../validation/RESULTS.md#security-pre-publication-fixes-on-dev--2026-10-02); of the four functions that ran only on custom-resource events or no known probe, the insight continuation worker logged to its own group in dev on 2026-10-04 ([validation results](../../validation/RESULTS.md#rescan-60e39b1-fixes-on-dev--2026-10-04)), and the seed function, its provider framework and the precompute function stay pending.

## Context

A `lambda.Function` built without a `role` gets a CDK-created role carrying the AWS-managed
`AWSLambdaBasicExecutionRole`, which allows `logs:CreateLogGroup`, `logs:CreateLogStream` and
`logs:PutLogEvents` on `*`. Any function could therefore write any log group in the account,
including the owned evaluation result group whose single writer
[ADR-079](adr-079-enable-online-evaluation-only-through-a-verifying-gate.md) depends on
(finding M-IAM-DISPATCH-LOGS-001, broadened). The repository already had the scoped pattern
(the ingestion functions grant only stream writes on their own declared group), but nothing
required it.

The dev synth showed 31 roles with the managed policy: 9 explicit roles that attached it by
name, 18 default roles, and 4 roles of CDK-internal functions (the user-pool client secret
getter's `AwsCustomResource`, two `cr.Provider` frameworks, and the site bucket's
`autoDeleteObjects` provider). The test configuration's eu-west-1 synth adds a fifth CDK-internal
role, the Frontend's `EdgeWebAclArnRead` reader, for 32.

## Decision

**No Lambda role carries a managed policy, and each writes only the log group its function
logs to.**

1. `infra/lib/mixins/logging.ts` provides `lambdaRole` (a role with no managed policy) and
   `grantOwnLogWrites` (`logs:CreateLogStream` and `logs:PutLogEvents` on `<group ARN>:*`).
   Every function role is built with `lambdaRole`; explicitly named roles keep their names,
   because resource policies identify them by name.
2. **One grant site per function:** whoever declares the function's log group grants on it,
   whether the role was passed in or created by a helper.
3. **No CDK-internal function keeps an unreadable role.** The client secret is read through an
   explicit `AwsCustomResource` with its own role, name and declared group, and data-hidden
   logging because the response carries the secret. Each `cr.Provider` framework gets a declared
   group and a `lambdaRole`. The Frontend site bucket drops `autoDeleteObjects`, whose core
   provider accepts no role, and `make destroy-all` empties the bucket instead.
4. **The gate, `infra/test/log-write-scope.test.ts`,** runs over every synthesised stack:
   R1 refuses any managed policy the gate cannot read (an AWS-managed policy by class), R2
   refuses any log write that is not on a same-template group or one of four reviewed pins
   (the evaluator's execution role, the evaluation input writer, and the Harness execution and
   provider roles), R3 judges what may reach the evaluation result group by action class
   (write, the seven event reads, 13 scoped custody actions, and the 4 unscoped custody actions
   no application role may hold), R4 does the same for Logs resource policies, and a positive
   control proves every function can still write its own group.

## Consequences

- The Frontend teardown depends on `make destroy-all` emptying the site bucket; a bare
  `cdk destroy` of the Frontend stack fails while the site is deployed.
- `logs:CreateLogGroup` is not granted. Each group is declared before its function exists, and a
  group deleted out of band is not silently recreated without retention: logging stops instead.
  If a function cannot log, the fallback is an own-group `logs:CreateLogGroup` grant, never the
  managed policy.
- The 18 default roles and the client-secret reader's role get new logical IDs, so the first
  deploy replaces them, and the function-change and role-change alarms fire once.
- A new function written without `lambdaRole` fails R1 at `make infra-test`.

## Alternatives considered

- **Keep the managed policy and detect cross-group writes.** Rejected: detection after the fact
  cannot make the result group's single writer structural.
- **Keep `autoDeleteObjects` and override the core provider's role in the template.** Rejected:
  the provider exposes no role, so the override is an escape hatch that breaks silently on a CDK
  upgrade, and dropping the property removes the provider entirely.

## Review trigger

A function that genuinely needs to write a log group it does not declare, or a CDK construct that
creates a function with no role prop.
