# ADR-018 — Encryption at rest

Date: 2026-08-19
Status: Accepted — implementation status corrected 2026-09-04; the 2026-09-26 ADR-079 exception withdrawn 2026-09-27

> **Amended 2026-09-27 — the ADR-079 key exception is withdrawn.** A dev experiment showed that a
> key policy does not bound readers of an encrypted CloudWatch Logs group
> ([ADR-079](adr-079-enable-online-evaluation-only-through-a-verifying-gate.md), amendment of
> 2026-09-27). The result groups keep CloudWatch Logs' default encryption, and this ADR still creates
> no application-owned key. The 2026-09-26 amendment below is superseded.

> **Amended 2026-09-26 by [ADR-079](adr-079-enable-online-evaluation-only-through-a-verifying-gate.md) — one scoped key exception (decided, not yet implemented).**
> Each online-evaluation result log group will be encrypted with its own application-owned
> customer-managed key. A CloudWatch Logs resource policy cannot restrict reads, and a key policy
> can, so the key is the only available reader boundary for those groups. The exception covers only
> those groups. The S3/Athena posture below and the service-managed DynamoDB posture do not change,
> and the released-response input group keeps the default encryption H-EVALUATION-EVIDENCE-KEY-001
> restored.

## Context

This ADR owns the S3 and Athena encryption posture for the analytical data path. Other data-bearing services use their stack-owned service encryption settings; they are not evidence that the S3/Athena path uses KMS.

| Option | KMS involved | Per-request cost | Key policy |
|---|---|---|---|
| **SSE-S3** | none | none | none |
| SSE-KMS with an AWS managed key | yes | charged per KMS call | AWS-controlled |
| SSE-KMS with a customer managed key | yes | charged per KMS call | customer-controlled |

A customer managed key provides an independent authorization layer, key-policy control, rotation policy, and key-level audit detail. It also requires complete grants for every producer, reader, and integrated service. Partial grants fail later as opaque access errors, so a customer-managed-key option is not safe merely because a bucket can be synthesized with one.

The platform is single-account per customer and does not move customer data across accounts. The main cross-account benefit of a customer managed key therefore has no current use. Existing S3 objects can move from SSE-S3 to SSE-KMS through a non-destructive controlled migration when that posture is later accepted.

## Decision

**Current DataV2 S3 and Athena storage uses SSE-S3.** The typed data configuration defaults to `s3_managed`; DataV2 buckets use S3-managed encryption and Athena result locations explicitly use `SSE_S3`. No application-owned KMS key is created, and application roles have no KMS decrypt grant for this path.

**Current DynamoDB state and evidence tables use service-managed AWS encryption.** Their table settings remain owned by their individual stacks. This is a separate service posture from the S3/Athena decision and does not create a repository-managed KMS key.

**`kms_cmk` is a declared future mode, not a deployable configuration today.** Selecting it intentionally fails synthesis until the complete V2 grant model, S3 and Athena configuration, tests, and non-production acceptance are implemented. A customer with a present compliance requirement for a customer managed key must not bypass that guard by editing resources manually; the repository must implement and prove the mode first.

**Customer-managed keys remain deferred, not rejected.** The enabling change must create the key, apply least-privilege grants to every required consumer, update the Athena workgroup result-encryption path, decide key rotation and migration behavior, use S3 Bucket Keys where applicable, and complete returned non-production acceptance before the mode can be selected.

## Consequences

- The present S3/Athena data path uses SSE-S3, not AWS-managed KMS encryption and not a customer-managed KMS key.
- The current platform has a mixed service posture: retained DynamoDB state and evidence tables use service-managed AWS encryption under their owning stacks.
- No independent customer-managed key-policy gate or customer-controlled key rotation exists in the current implementation. This is an accepted reduction in defence-in-depth and must be stated honestly in security review.
- The IAM surface remains smaller for the S3/Athena path: no KMS grants or key policy can be incomplete there.
- `G-07` remains open for secret rotation and for any future customer-managed-key design; S3-managed data encryption itself has no repository-owned rotation control.
- `DataV2Stack`, not a legacy Foundation or Data stack, owns the current retained analytical storage boundary. [ADR-012](adr-012-stack-composition-and-config.md) owns the complete stack composition.

## Alternatives rejected

**SSE-KMS with the AWS managed key.** Rejected for the S3/Athena path because it adds KMS request cost while providing no editable key-policy control over SSE-S3.

**Customer-managed keys before complete consumer grants exist.** Rejected because partial adoption produces a deployment that appears encrypted but fails at first use.

**Treating `kms_cmk` as customer configuration before it is implemented.** Rejected because a config value that cannot safely deploy must fail at synthesis, not defer failure into a customer environment.

## Review trigger

Reopen when all of the following are available for a non-production deployment:

- a complete least-privilege grant and key-policy model for every V2 S3, Athena, and consuming service path;
- tested migration and rollback behavior for retained data;
- explicit key rotation and S3 Bucket Key policy; and
- returned deployment and runtime acceptance evidence for the customer-managed-key mode.

A proposed cross-account customer-data path also reopens this decision because it would alter the threat model the SSE-S3 decision relies on.
