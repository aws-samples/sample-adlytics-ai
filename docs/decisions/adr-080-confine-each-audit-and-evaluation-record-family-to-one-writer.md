# ADR-080: Confine each audit and evaluation record family to one writer

Date: 2026-09-26

Status: Accepted 2026-09-26 — partly implemented; current state in the
[Agent Quality Loop implementation status](../capabilities/agent-quality-loop/low-level-design.md#record-family-storage-mandated-by-adr-080).
It amends
[ADR-059](adr-059-create-a-bounded-agent-quality-audit-loop.md) (the storage boundary) and
[ADR-064](adr-064-retire-planner-era-contracts-and-anchor-the-audit-trajectory.md) (where each
companion item lives), and closes the write-separation half of H-EVALUATION-RESULT-001; its
deployment state is the
[Agent Quality Loop implementation status](../capabilities/agent-quality-loop/low-level-design.md#record-family-storage-mandated-by-adr-080). The mandated storage contract is owned by the
[Agent Quality Loop design](../capabilities/agent-quality-loop/low-level-design.md). Dated
validation is owned by [validation results](../../validation/RESULTS.md).

## Context

The agent-quality audit table holds six record families under one partition key, `audit_id`:

- the receipt, its delivery disposition, and its evaluation admission, all written by the
  conversation route;
- findings, written by the processor;
- evaluation delivery receipts, written by the outbox dispatcher;
- writer receipts, written by the evaluation writer;
- verdicts, written by the result linker.

Five roles hold `dynamodb:PutItem` on the whole table. The IAM policy simulator shows each allowed,
and update, delete, and batch write implicitly denied [verified 2026-09-26, IAM policy simulator].
Writes are first-writer-wins, so any of the five roles can pre-write another family's item and make
the genuine write fail as a conflict. For a verdict, that is a forged evaluation. For a writer
receipt, it redirects which evaluation result the ingress links to the turn, because the ingress
matches on the writer receipt's trace identifier.

IAM cannot separate the families inside one table. `dynamodb:LeadingKeys` constrains the partition
key only, and there is no sort-key condition
([fine-grained access control](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/specifying-conditions.html))
[verified]. The families share a partition.

The writer and the linker also lack `dynamodb:GetItem` [verified 2026-09-26, IAM policy simulator],
so an at-least-once replay of their conditional write cannot read the stored item to recognise
itself. It is reported as unreadable and redriven to the dead-letter queue instead of being
classified as an exact replay.

## Decision

**Every per-turn audit and evaluation record family lives in a table whose resource policy admits
exactly one writer role and denies modification and export to everyone. Each writer can read back
what it wrote. Control-plane changes to those tables raise an alarm.**

### 1. One table per writer

| Table | Families | Only writer |
|---|---|---|
| Receipts (the existing audit table) | receipt, delivery disposition, evaluation admission | Conversation function role |
| Findings | findings | Agent-quality processor role |
| Evaluation deliveries | evaluation delivery receipts | Outbox dispatcher role |
| Writer receipts | evaluation writer receipts | Evaluation writer role |
| Verdicts | evaluation results | Result linker role |

- Receipts and findings live in the retained agent-quality stack; the three evaluation tables live
  in the retained evaluation evidence stack.
- Every role a table policy names, writer or reader, has a fixed name, so the policy can name it
  before the role's stack deploys, as
  [ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) §3 does for
  the route exchange.
- A writer that is not a receipts reader gets the receipt's creation time on the message it
  already receives, the outbox envelope or the result projection, so it can compute its record's
  expiry without reading the principal-bearing table.
- **Order inside the slice:**
  1. the new tables, the fixed role names, and every writer and reader moved to its table, with no
     resource policies yet;
  2. the agent-quality processor's stream mapping filtered to `INSERT` events, so neither the
     backfill's updates nor TTL deletions reach it. Today it consumes every event and retries a
     receipt it cannot find;
  3. TTL enabled, then §5's backfill;
  4. the resource policies and the item-change pipes of §4.

  A policy that landed before step 1 would refuse the still-deployed old writers mid-deploy.
- The ingress, which admits results by the turn's delivery receipt
  ([ADR-079](adr-079-enable-online-evaluation-only-through-a-verifying-gate.md) §3), reads the
  evaluation deliveries table.
- Every table keeps `audit_id` as its partition key and the existing item-key shapes, so readers
  change table, not query.
- Every table declares on-demand billing, AWS-managed encryption, point-in-time recovery, deletion
  protection, and the retained removal policy.
- **Retired in the same slices:** every writer's `PutItem` grant on a table it no longer writes, the
  stream noise those writes caused on the receipts table, and the readers' queries against the old
  locations.

### 2. The resource policy

Every table's policy denies, for every principal whose `aws:PrincipalArn` is not named:

1. `PutItem` and `PartiQLInsert`, unless the principal is the table's writer role. An explicit deny
   in a resource policy overrides an identity allow in the same account, and `TransactWriteItems` is
   authorized per item by the underlying action
   ([resource-based policies](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/access-control-resource-based.html))
   [verified].
2. `BatchWriteItem`, `UpdateItem`, `DeleteItem`, `PartiQLUpdate`, and `PartiQLDelete`, to everyone,
   the writer included. `BatchWriteItem` is one action for batched puts and deletes, and no writer
   needs it. Records are immutable, and expiry is the service's TTL deletion, which item policies do
   not govern.
3. Every action that copies the table's data out, to everyone: `CreateBackup`,
   `RestoreTableToPointInTime`, `ExportTableToPointInTime`, and the Kinesis streaming-destination
   actions. CloudFormation never needs them. (Amended 2026-09-27: AWS Backup's
   `StartAwsBackupJob` and `RestoreTableFromAwsBackup` were listed here, but DynamoDB rejects them
   as invalid resource-policy actions [verified, a deploy's `PutResourcePolicy` refusal]. An AWS
   Backup job is therefore detected by §4's alarm, not prevented.)
4. Every control-plane action that could change or stop protecting the table, unless the principal
   is the stack's CloudFormation execution role, taken from the stack's synthesizer: resource-policy
   changes, `UpdateTable`, `DeleteTable`, `UpdateTimeToLive`, and `UpdateContinuousBackups`.

Each statement covers the table and its indexes. The receipts table, which alone holds the
principal, also denies every read action to every principal except the roles
[the design](../capabilities/agent-quality-loop/low-level-design.md) names as its readers. No
administrator's identity policy can read who asked directly, and live acceptance reads receipts
through the review API rather than directly.

- The policies bind roles, not code. An administrator who can pass a named role to a new function,
  or change a named function's code or configuration, acts as that role. A CloudTrail-driven alarm,
  §4's mechanism, fires on function creation, code or configuration changes, and role passes for
  every function that holds a named role.
- Denying `RestoreTableToPointInTime` to everyone means a point-in-time recovery first needs a
  policy change through CloudFormation, which raises §4's alarm.
- The processor's aggregate table holds derived measurements, not a turn's records. It keeps its
  one writer by IAM only and is outside this set.

- The account root can always delete a resource policy, and CloudFormation does not detect drift on
  one [verified, resource-based policies]. Each deploy reads every policy back, and §4 alarms between
  deploys.
- Whether TTL deletion proceeds under a policy that denies `DeleteItem` to everyone is [unverified].
  §5's legacy expiry is the live proof, and the design is revisited if it fails.
- The evaluation outbox table is not in this set. It is transient, content-bearing transport with
  two writers by design, the route and the dispatcher's lease, governed by
  [ADR-059](adr-059-create-a-bounded-agent-quality-audit-loop.md)'s outbox design.

### 3. Read back your own write

Every writer holds `dynamodb:GetItem` on its own table, so a replayed conditional write compares the
stored item with its own and reports an exact replay.

### 4. Detect what the policy cannot prevent

- **Item changes.** Each table's KEYS_ONLY stream feeds an EventBridge pipe filtered to `MODIFY` and
  `REMOVE` records that are not TTL deletions; TTL deletions carry the DynamoDB service principal in
  their user identity
  ([TTL and streams](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/time-to-live-ttl-streams.html))
  [verified]. The pipe writes to a log group, and a metric filter alarms on any record. Whether a
  pipe can target CloudWatch Logs directly is [unverified]; if it cannot, the target is the smallest
  function that emits the metric.
- **Control-plane changes.** An EventBridge rule on CloudTrail management events alarms on every
  §2 statement 3 and 4 action against these tables, whoever calls it, the CloudFormation execution
  role included. A deploy that changes a policy, TTL, or point-in-time recovery setting therefore
  raises an expected alarm, which the deploy record acknowledges. EventBridge receives CloudTrail
  events only while a trail with logging is active in the account
  ([CloudTrail events in EventBridge](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-service-event-cloudtrail.html))
  [verified]. The stack does not create a trail; the deploy check reports when none is logging.

### 5. The legacy items

The findings, evaluation-delivery, writer-receipt, and verdict items already in the audit table are
not migrated and not read. They are dev data from before this change.

Before the resource policy lands, and after TTL is enabled on the table, a one-time backfill gives
every item in the audit table an expiry:

- a receipt, its delivery disposition, and its admission get the receipt's creation time plus the
  configured retention, so
  [ADR-081](adr-081-make-every-turn-traceable-and-attributable.md)'s retention reaches receipts
  written before it, including the principal-bearing ones written since 2026-09-26;
- a legacy item of a family that has moved out gets an expiry a declared margin after the policy
  deploy, so TTL removes it once the policy is in force. Its removal, counted only after the policy
  deploy and separately from receipts, is the live proof that TTL deletion is not blocked by the
  deny.

The backfill is the only update ever made to these items, and it runs before the policy that
forbids updates. A deployment upgrading from before this change runs the same backfill, in the
[upgrade order](../capabilities/agent-quality-loop/low-level-design.md#record-family-storage-mandated-by-adr-080)
the Agent Quality Loop design owns.

## Consequences

- A compromised writer can forge only its own family. The verdict a reviewer reads was written by the
  linker, and the receipt by the route.
- An administrator can no longer read the principal or copy a table out at all, and can switch off
  point-in-time recovery or change expiry only through CloudFormation or as root. Every such change
  raises an alarm.
- A reviewer reading one turn reads five tables instead of one. Each read is an exact key read or a
  key-bounded query, as today.
- Four more retained tables, four more fixed role names, and one repeated construct. The policy shape
  is uniform, so one construct and one test pattern cover them all.
- Administrators lose the ability to repair a record in place. A wrong record is superseded by a new
  record, never edited.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Keep one table and add `LeadingKeys` conditions | Constrains only the partition key, which the families share. |
| Move only verdicts out, and share one pipeline table among the dispatcher, writer, and processor | Leaves the writer receipt forgeable by the dispatcher, and the writer receipt decides which result links to a turn. |
| Sign every record with KMS and verify on read | Detects forgery but still lets a pre-written item block the genuine one. A later addition if customers need evidence that is tamper-evident against administrators. |
| AWS Backup vault lock for write-once copies | Conflicts with the declared retention of ADR-081. |
| An attribute-name split inside one table | The item key can still be squatted. |

## Review trigger

Revisit when a record family gains a second legitimate writer, when DynamoDB adds sort-key conditions
to IAM, or when customers require evidence that holds against account administrators.
