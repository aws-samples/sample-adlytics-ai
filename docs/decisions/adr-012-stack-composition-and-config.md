# ADR-012 — Stack composition, and SSM for every cross-stack value

| | |
|---|---|
| **Status** | Accepted — superseded in part by ADR-015 |
| **Date** | 2026-08-18 |
| **Decision** | **Stacks are bounded by lifecycle and blast radius, not by service. Deploy-time values cross stack boundaries through SSM Parameter Store, never CloudFormation exports or construct references. A deterministic typed resource name may be derived only where least-privilege IAM requires a synth-time ARN.** No environment-derived URL has a fallback. |
| **Related** | [repo structure](../repo-structure.md) §3 · [ADR-005](adr-005-compute-runtime-baseline.md) · [ADR-008](adr-008-frontend-hosting.md) · [ADR-018](adr-018-encryption-at-rest.md) |

> **Amended 2026-10-01.** `infra/bin/app.ts` now chains every composed stack with an order-only dependency derived from `DEPLOY_ORDER` ([`infra/lib/deploy-order.ts`](../../infra/lib/deploy-order.ts)), so `cdk deploy --all`, which `make deploy-all` runs, follows it. Values still travel only through SSM and no template changes; `make deploy` deploys one stack with `--exclusively`. The "automatic deploy ordering" row below describes the state before this amendment.
>
> **Superseded in part by [ADR-015](adr-015-no-remote-ci.md) (2026-08-18).** There is no deployment pipeline: `make` is the gate and a human runs the [manual deployment sequence](../../README.md#deploy). This ADR does not duplicate that sequence. The stack-boundary and SSM decisions remain current.

## Context

This reverses the earlier direct-construct-reference rule. That CDK default is wrong for this platform:

- Removing an export/import reference is a two-deploy operation that can deadlock an update.
- CloudFormation cannot update or delete an export while another stack imports it.
- CloudFormation exports cannot cross Regions, while frontend infrastructure can require a cross-Region certificate.
- A consumer must fail for a missing deployment value instead of silently building against `localhost` or another fallback.

## Decision

### Deploy-time cross-stack values go through SSM

A producing stack writes a `StringParameter`; a consuming stack reads it during deployment. There is no direct CloudFormation dependency between application stacks.

```
/adlytics/<environment>/<area>/<name>
```

[`PARAMS` and `paramPath()`](../../infra/lib/config/index.ts) own the complete typed **SSM** interface: every parameter path and its producing stack. Call sites never construct a path literal. Adding, renaming, or retiring an SSM interface changes that owner and its consumers together.

**Exception — synth-time IAM resource identities.** A deploy-time SSM token cannot reliably form the resource ARN required by a least-privilege IAM statement. Where a consumer needs that ARN at synth, it derives the exact deterministic physical name or secret identifier from the typed configuration helper. This does not authorize construct references, exports, literals, or wildcard IAM resources.

**What this costs:**

| Lost | Consequence |
|---|---|
| Automatic deploy ordering | The operator follows the [manual deployment sequence](../../README.md#deploy); CDK cannot infer SSM producer/consumer order. |
| Automatic propagation | A changed parameter requires redeploying every consumer. |
| Synth-time producer-value knowledge | `valueForStringParameter()` resolves at deploy, so an SSM value cannot drive conditional synthesis. A missing produced parameter fails the consuming deployment. |

The trade is deliberate. Direct operator configuration that is unset fails synthesis; a missing producer parameter fails the consuming deployment. Neither condition is a reason to substitute `localhost` or another fallback.

Never use `valueFromLookup()` for a value this app produces. It caches a synth-time value into `cdk.context.json` and can silently serve an obsolete producer value.

### No environment-derived URL may have a fallback

A missing URL, hostname, user-pool ID, or client ID is a configuration failure. Direct operator configuration is validated before synthesis; produced SSM values resolve during deployment. Neither may substitute a local or development value.

The frontend is a static export, so it is built only after the required infrastructure values exist. A first deployment can require the deliberate two-pass frontend/API attachment in the [manual deployment sequence](../../README.md#deploy).

### Stacks are bounded by lifecycle and blast radius

The current physical stack inventory, conditional composition, and state policy are owned by [repo structure](../repo-structure.md#current-stack-layout), which derives them from [`infra/bin/app.ts`](../../infra/bin/app.ts). This ADR deliberately does not duplicate that current-state inventory. The composition decision applies the following principles, in priority order: No application-owned KMS key or `FoundationStack` exists in the current CDK app. The current S3/Athena path uses SSE-S3: DataV2 buckets use the `s3_managed` configuration and Athena result locations explicitly use `SSE_S3`. Retained DynamoDB state and evidence tables use service-managed AWS encryption under their owning stacks. Selecting `kms_cmk` fails synthesis intentionally because the complete V2 grants and workgroup configuration have not been implemented. A customer-managed-key mode is a future explicit change under [ADR-018](adr-018-encryption-at-rest.md), not an available configuration today.

Principles, in priority order:

1. Never put a retained resource in the same stack as disposable infrastructure.
2. Create customer and conversation state explicitly; never accept a service-created side effect as the only copy of state that must survive a replacement.
3. Separate by rate of change after the retention boundary is safe.
4. Never split a stack merely to escape a circular dependency; correct the boundary instead.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Direct CDK construct references | Create CloudFormation export coupling and cross-Region limits. |
| Hand-written `Fn::ImportValue` | Keeps the same export coupling without typed configuration ownership. |
| One monolithic stack | Eliminates cross-stack interfaces at the cost of combining unrelated blast radii. |
| `valueFromLookup()` for in-app values | Caches producer values during synth and can silently serve stale configuration. |
| Nested stacks | Share a parent lifecycle and do not create the required retention boundary. |
| URL or identity fallbacks | Turn a missing deployment value into a plausible but incorrect environment. |
| Customer-managed KMS keys now | Rejected until every V2 consumer and Athena result path has the required grants and the mode can be proven through deployment and acceptance. |

## Consequences

- Follow the [manual deployment sequence](../../README.md#deploy). Run synth and diff for each applicable stack and read the result before deployment.
- A fresh-account deployment creates all repository-owned resources through CDK. Account bootstrap, explicit typed deployment configuration, customer source access, and customer-owned external roles/IDs remain documented external prerequisites; they are never discovered through account-wide enumeration.
- A changed SSM value requires redeploying its consumers. This is accepted because the interfaces are near-static and typed.
- The deployment identity must restrict `ssm:GetParameter` to the exact paths it resolves. Current stacks use CloudFormation dynamic references and do not declare that grant themselves; the deployment-role policy is the enforcement point, never a wildcard over `/adlytics/*`.
- Renaming an SSM path is a staged producer/consumer migration: publish the new path, deploy all consumers, then retire the old path.
- Current CDK source defines the intended composition. Claims about a particular environment’s deployed state require scoped CloudFormation and exact resource reads for that environment.

## Review trigger

- a parameter changes frequently enough that deployment-time SSM reads become operationally burdensome;
- a cross-stack value becomes a secret and must move to Secrets Manager;
- a deployable ML stack is added to `infra/bin/app.ts`;
- a customer-managed-key mode has complete S3, Athena, and consumer grants; tests; and non-production acceptance evidence; or
- a stack introduces a retained resource into a disposable lifecycle boundary.
