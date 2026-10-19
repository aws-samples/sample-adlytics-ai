# ADR-079: Enable online evaluation only through a verifying gate

Date: 2026-09-26

Status: Accepted 2026-09-26 — implemented in the repository 2026-09-27; result-custody live
acceptance recorded in dev 2026-10-02 (A0–A5, [validation results](../../validation/RESULTS.md#security-pre-publication-fixes-on-dev--2026-10-02)); current state in the
[Agent Quality Loop implementation status](../capabilities/agent-quality-loop/low-level-design.md#record-family-storage-mandated-by-adr-080)). It amends
[ADR-059](adr-059-create-a-bounded-agent-quality-audit-loop.md) (where evaluation results are
written, what enables evaluation, and the regression path) and
[ADR-018](adr-018-encryption-at-rest.md) (one application-owned key per evaluation result group,
withdrawn 2026-09-27),
and closes the self-attestation half of H-EVALUATION-RESULT-001; its deployment state is the
[Agent Quality Loop implementation status](../capabilities/agent-quality-loop/low-level-design.md#record-family-storage-mandated-by-adr-080). The write-separation half is
[ADR-080](adr-080-confine-each-audit-and-evaluation-record-family-to-one-writer.md). The mandated
contract is owned by the
[Agent Quality Loop design](../capabilities/agent-quality-loop/low-level-design.md) and, for result
custody, by [observability](../concepts/observability.md). Dated validation is owned by
[validation results](../../validation/RESULTS.md).

> **Amended 2026-09-27 — no result-group key; four delivery clarifications; three implementation
> notes (§1, §2, §3, §4, §6).** Text below that this amendment replaces is marked "Superseded
> 2026-09-27" in place.
> - **The key is withdrawn (§1).** A dev experiment falsified the premise that a key policy bounds
>   readers of an encrypted group. CloudWatch Logs refuses to associate a key whose policy lacks its
>   service-principal grant. With that grant in place, the operator read the group through
>   `GetLogEvents` and Logs Insights with no key grant of its own, for 40 minutes under an explicit
>   key-policy deny of its own `kms:Decrypt`, including an event written after the deny [verified
>   2026-09-27, dev, throwaway group and key]. The CloudWatch Logs
>   guide says a caller needs its own KMS permissions to read or write an encrypted group; the
>   observed behaviour differs, and this decision follows the observed behaviour. KMS also refuses a
>   key policy naming a role that does not yet exist [verified 2026-09-27], which would have
>   bound the retained stack to the destroyable gate stacks. Readers of a result group are
>   therefore bounded by IAM: no application role holds a read of it, which the gate's acceptance
>   checks with the IAM policy simulator. An account administrator can read it, as it can read the
>   outbox that holds the same content. ADR-018's key exception is not taken.
> - **One row, every enabled family (§3).** One outbox row becomes one released span, and every
>   enabled family's configuration reads the same input group. The delivery receipt therefore names
>   every family enabled at dispatch, each with its configuration identifier and evaluator set.
> - **A bounded wait for the delivery companion (§4).** The route admits a row before its stream
>   ends and writes the ADR-063 companion afterwards. A row whose companion is absent waits until
>   its admission time plus the conversation function's timeout and a margin, then ends not
>   dispatched as `delivery_unknown`.
> - **Pre-gate records (§4).** A delivery receipt written before this change is classified as
>   predating the gate by its schema version and never decoded as a current receipt; verdicts
>   written before it are not read, as the Consequences already state.
> - **Ingress invocation (§6).** The result ingress's resource policy denies `lambda:InvokeFunction`
>   and `lambda:InvokeAsync` to every caller whose source ARN is not an owned result group, except
>   Lambda's own service principal, which runs each subscription delivery and re-evaluates the
>   policy as itself; without that exemption every delivery was dropped [verified 2026-09-27, a dev
>   probe stack]. So only CloudWatch Logs delivering from those groups invokes it, on the same
>   [unverified] assumption as the gate provider below. This retires the directed acceptance path.
> - **The SSM rename takes one deploy pass (§6), a deliberate deviation.** §6 lands the new
>   result-group keys, redeploys their readers, and removes the old keys in a later deploy. The
>   implementation does all three in one pass of the typed deploy order. The evidence stack
>   publishes the new keys first, the result-link stack (their reader) redeploys onto them next,
>   and the evaluation stacks that produced the old keys remove them last. The second deploy
>   existed to protect a reader that still resolves the old keys. Whether a rollback of the
>   result-link stack to its previous template could still resolve them after the pass is
>   [unverified]; that rollback is the risk this deviation accepts.
> - **Evaluator identity is its prompt and model (§3).** An evaluator's revision is a digest of its
>   instructions and model identifier, and its name carries that revision. Any prompt or model edit
>   therefore replaces the evaluator and changes the gate's specification. §3's evaluator-set change
>   order then governs every prompt or model change, not only an explicit revision.
> - **The gate provider's invocation boundary (§2).** The provider's resource policy denies
>   `lambda:InvokeFunction` and `lambda:InvokeAsync` to every principal but the stack's
>   CloudFormation execution role and Lambda's own service principal, which runs the queued
>   asynchronous event, and the provider makes no asynchronous retries. CloudFormation invokes as
>   its execution role, asynchronously, and Lambda then re-evaluates the policy as its own service
>   principal when it runs the event [verified 2026-09-27, the first dev gate deploy and a probe
>   stack]. A direct caller, synchronous or asynchronous, is refused. The exemption names a
>   principal, not an event, so only a deployment runs a gate on the assumption that no other
>   invocation Lambda makes on its own behalf reaches the provider [unverified]. The one such path
>   in view, an event-source mapping, needs a source the provider's role can read, and it holds
>   none; creating or repointing a mapping at a watched function raises the function-change alarm.
>   No retry reruns a gate after it has answered.

> **Amended 2026-09-27 — the V3 family is retired; V4 is the only family (§1, §2, §3, §6).** The
> owner decided on 2026-09-27 that a superseded version is retired explicitly and entirely once its
> successor exists. The V4 family (grounded correctness, internal consistency, helpfulness)
> supersedes the V3 single released-response judge. Text below that names two families now
> describes one; the per-family mechanism stays, so a successor to V4 is added as a family and
> retires V4 the same way.
> - **Removed:** V3's stack, evaluator, prompt, calibration corpus, gate, configuration, release
>   record, owned result group and its subscription filter, and its request flag. Its entries in
>   the typed deploy order, the dispatcher's release list, and the record-integrity watch are also
>   removed. The retained V3 result group is deleted under this decision, with its deletion
>   protection lifted. Earlier evaluator revisions that CloudFormation retained on replacement are
>   deleted too, as are the service-created groups (§6).
> - **One provisioning flag.** `onlineEvaluation.provisioned` composes the result link and the V4
>   family, and, as V4 provisioning already was, may be set only for dev. The separate V4
>   provisioning flag is retired. The V4 request flag stays, still dev-only.
> - **Stored records stay readable until they expire.** Delivery receipts and verdicts written
>   while V3 ran name it. The family enumeration keeps the V3 value for decoding those records
>   only. No gate specification, release record, release parameter, or deploy-time family list
>   accepts it. With its configuration deleted and its group unsubscribed, nothing dispatches to V3
>   or admits a V3 result. The value is removed once the last such record has expired: receipt
>   retention after the retirement deploy, 2026-12-26 in dev at 90 days.
> - **Other environments.** Only dev has been deployed from this repository [unverified beyond the
>   dev account], so no other environment has a V3 family to retire. One that provisioned V3 under
>   the old allowance now fails synth while the provisioning flag is set. It unsets the flag,
>   deletes its V3 stack and then its result-link stack directly (the app no longer composes
>   either there), and redeploys the evidence stack, which orphans the V3 group for deletion.
> - **Retirement order.** The V3 stack is deleted first, while its provider still runs code that
>   knows V3. Its Delete writes the release record not enabled, then deletes the configuration and
>   the record. Next, the result link stops subscribing and reading the V3 group. The evidence
>   stack then stops declaring the group and its parameter. The dispatcher, the record-integrity
>   watch, and the V4 gate follow. The orphaned group is deleted last. Consumers stop reading
>   before a parameter is removed, as the rename rule requires. This is the reverse of the typed
>   deploy order for the evidence stack and the result link, so it is a stated one-time sequence,
>   not a change to the typed order. Lifting the group's deletion protection and deleting it raise
>   the result-group alarm by design.

> **Amended 2026-10-04 — tag-bound creates and profile-bound judge grants (§2).** Findings
> H-IAM-AGENTCORE-CREATE-001 and A-IAM-MODEL-001.
> - **The create carries the standard tags.** `CreateOnlineEvaluationConfig` has no resource type,
>   so the provider role may create only on `*` with exactly the application's request-tag set,
>   and `TagResource` on `*` with the same set and no other application's or environment's
>   resource tags. The gate specification gains one optional member, `tags`, which the create
>   sends and an update never does. It is optional so that a rollback, which delivers the
>   properties of the template before the member existed, still parses. An untagged create is
>   refused by IAM, so a missing tag set fails closed [verified 2026-10-04, dev: the gate's create
>   after an out-of-band delete carried the three tags and succeeded].
> - **Judge grants name the evaluator models only.** The execution role's judge grant narrows from
>   any model to the family's evaluator models. Both roles hold each evaluator's inference profile
>   in the stack's Region, and its foundation model in any Region only through that profile
>   (`bedrock:InferenceProfileArn`). `Evaluate` runs the judge under the calling provider's role
>   [verified 2026-10-04, CloudTrail `invokedBy bedrock-agentcore.amazonaws.com`], which is why the
>   provider keeps its calibration grant.

## Context

Online evaluation has two switches that nothing reconciles:

- **Enablement** is `ExecutionStatus` on the CloudFormation `OnlineEvaluationConfig`, set from
  environment flags that include an operator's attestation that the result group's retention was
  checked.
- **Capture** is whether the conversation route writes released turns to the evaluation outbox. It
  is decided at synth time from a different environment flag, because the outbox grant and table
  name render only when that flag is set.

Each `make deploy` synthesizes the whole application from its own shell, so one stack can deploy
with evaluation enabled while another deploys with capture off. That happened in dev: both
evaluators are enabled at full sampling, and no turn has been captured since 2026-09-15 because the
API was deployed without the capture flag [verified 2026-09-26, deployed state]. The configuration
validator compares one shell's flags with each other and never with the deployed state.

Evaluation results go to a group the service creates under the reserved
`/aws/bedrock-agentcore/evaluations/` prefix, so no stack sets its retention, key, or deletion
protection. The service API accepts a customer-supplied destination
(`outputConfig.cloudWatchConfig.logGroupName`) and uses an existing group as-is, while the
CloudFormation resource has no property for it [verified 2026-09-26, AgentCore control-plane API
reference and the `AWS::BedrockAgentCore::OnlineEvaluationConfig` resource reference].

Two CloudWatch Logs facts decide how readers can be bounded:

- A Logs resource policy can grant only `logs:PutLogEvents` and `logs:CreateLogStream` to service
  principals, never reads
  ([PutResourcePolicy](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutResourcePolicy.html))
  [verified].
- On a group encrypted with a customer-managed key, a principal calling `GetLogEvents`,
  `FilterLogEvents`, or `StartQuery` needs `kms:Decrypt` through CloudWatch Logs, and the log group
  ARN is part of the encryption context
  ([encrypt log data with KMS](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/encrypt-log-data-kms.html))
  [verified]. The key policy is therefore an executable reader boundary. *(Superseded 2026-09-27:
  a dev experiment falsified this; see the amendment above.)*

[ADR-018](adr-018-encryption-at-rest.md) creates no application-owned key, and
H-EVALUATION-EVIDENCE-KEY-001 removed one from the released-response input group because nothing
there needed it. A result group is different: its key is the only control that can bound its
readers. *(Superseded 2026-09-27: a key does not bound them; see the amendment above.)*

## Decision

**A custom resource is the only owner of each online-evaluation configuration. It binds the
configuration to a stack-owned, key-encrypted result group *(Superseded 2026-09-27: the group has no
key; see the amendment above)*, reads that group's controls back, and only then enables
evaluation. It publishes the verified state as a release record, which the
outbox dispatcher reads on every run to decide what reaches evaluation.**

### 1. Owned result groups

- The retained evidence stack creates one result group per evaluation family, outside the reserved
  prefix, with retention from the typed configuration and log-group deletion protection, which
  CloudWatch Logs and CloudFormation support [verified 2026-09-26, CloudWatch Logs and
  `AWS::Logs::LogGroup` references].
- *Superseded 2026-09-27 by the amendment above: the result groups have no key, and readers are
  bounded by IAM.* Each group has its own customer-managed key with exactly three key-policy grants: the CloudWatch
  Logs service principal may use the key only under that group's `aws:logs:arn` encryption
  context; the stack's CloudFormation execution role may administer the key but never encrypt,
  decrypt, re-encrypt, or create grants; and the gate provider role may only read the key's policy,
  description, grants, and tags, so it can verify the key before enabling. There is no account-root
  or identity-policy delegation, so no application role or administrator's identity policy can
  decrypt. Reading raw
  results directly therefore takes a key-policy change, which CloudTrail records. If the
  administering role were lost, recovery would need AWS Support [unverified].
- *Superseded 2026-09-27 by the amendment above, except that the gate still reads back, and the
  CloudTrail-driven alarm still watches, every non-key path listed here.* The key bounds direct
  reads, not every path to plaintext. A new subscription filter, an export
  task, Live Tail, or disassociating the key could each expose results without a key-policy change;
  whether export and Live Tail need `kms:Decrypt` on an encrypted group is [unverified]. The gate
  reads those settings back on every deploy, and a CloudTrail-driven alarm, the same mechanism as
  [ADR-080](adr-080-confine-each-audit-and-evaluation-record-family-to-one-writer.md)'s, fires
  between deploys on subscription-filter, export, Live Tail, key-association, account-policy,
  data-protection, retention, and log-group deletion changes, and on key-policy, grant, disable,
  and deletion-schedule changes to the key.
- No application role holds `logs:PutLogEvents` on a result group, so the result ingress links
  only what the evaluation service wrote.
- *Superseded 2026-09-27 by the amendment above: the exception is not taken.* This is the one
  exception to ADR-018 for encryption at rest: an application-owned key where the
  key is the only direct-read boundary available.

### 2. The gate owns the whole configuration

- `Custom::AgentCoreOnlineEvaluation` creates, updates, and deletes each configuration through the
  AgentCore control-plane API, as the Harness provider does for the Harness
  ([ADR-077](adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md) §4). The
  CloudFormation `OnlineEvaluationConfig` resources are removed.
- The gate creates each configuration under a new name, so its create never collides with the
  retiring CloudFormation configuration, which the same deploy deletes in its cleanup phase.
- A digest of the evaluation specification and the desired state is a resource property, so every
  change reruns the gate.
- The result ingress, its subscription filters, and the linker deploy before the gate stacks. The
  filters need only the owned group names, which the evidence stack publishes, so the gate can
  assert them without a stack cycle.
- On every run the gate:
  1. reads the configuration, the group, and the family's release parameter (§3);
  2. **when execution is not requested:** writes the release parameter as not enabled first, then
     sets the configuration `DISABLED` bound to the owned group, and reads both back. It stops here;
  3. **when execution is requested:** if the binding or any control in step 4 does not hold, writes
     the release parameter as not enabled, sets the configuration `DISABLED`, and binds the owned
     group. A configuration whose binding and controls already hold is left running, so a routine
     deploy opens no evaluation gap;
  4. asserts by readback that the configuration names the owned group; the group's retention,
     deletion protection, and key match configuration *(superseded 2026-09-27: the group must have
     no key)*; its only subscription filter is the result
     ingress's; no account-level subscription or data-protection policy, delivery, or export task
     reaches it; and no Logs resource policy grants anyone but the evaluation service write access
     to it;
  5. runs the family's calibration corpus (§5) if the evaluation API accepts it, sets `ENABLED`,
     reads it back, and only then writes the release parameter as enabled.
- Any failed assertion or calibration leaves the configuration disabled, its release parameter not
  enabled, and the deploy failed. Delete writes the release parameter as not enabled, disables the
  configuration, then deletes both.
- If the service refuses the owned group or its key *(superseded 2026-09-27: there is no key)*,
  evaluation stays disabled and the gap is recorded. There is no fallback to the service-created group.
- Execution is requested by one typed, dev-only configuration value per evaluation family. It
  replaces the per-family enabled flags, the retention-confirmation flags, and the capture flag.

### 3. One release parameter per family

- Each family's gate is the only writer of one SSM parameter, the family's release record. It says
  whether the family is enabled and, when it is, names the configuration identifier and the
  evaluator set with each evaluator's revision, model identifier, and prompt digest. It says
  enabled only after the gate read back `ENABLED`. The gate provider creates and deletes it; no
  CloudFormation resource declares its value.
- A missing release record means not enabled. A write to a release record by any principal but the
  gate provider raises the control-plane alarm.
- **The dispatcher is the switch.** The conversation route admits every released turn to the outbox
  whenever the evaluation plane is provisioned, with no switch of its own: request-serving settings
  are resolved once per container, so a route-side switch would go stale in warm containers. The
  outbox dispatcher, a scheduled job, reads the release records on every run. A row whose family is
  enabled is sent, and its delivery receipt records the configuration identifier and evaluator set
  it was sent to. Any other row ends in the terminal not-dispatched state of §4.
- The result ingress admits a result only when its configuration identifier matches the turn's
  delivery receipt, and stamps provenance from that receipt, so a late result keeps the evaluator
  revision it was produced under and is not refused after a later disable.
- The route's outbox grant and table name render whenever the evaluation plane is provisioned, so
  capture no longer depends on the shell that deployed the API.
- An alarm fires when a family is enabled and the dispatcher has sent no turn for a configured
  interval while turns complete. A live wiring probe checks both directions on demand: each release
  record against its configuration's deployed execution state.
- **Changing a family's evaluator set** follows one order: the release record says not enabled, the
  configuration is disabled, the evaluators are updated and calibrated, the configuration is
  enabled and read back, and only then is the release record rewritten.

### 4. Honest states, dispatch, and provenance

- The review projection reports a turn's evaluation as `results_available`, `partial` (some
  evaluators have reported), `overdue` (past the configured deadline with no result), or
  `not_selected` with a closed reason. The evaluator set is the one recorded on the turn's delivery
  receipt, so the review function needs nothing from stacks that deploy after it.
- The dispatcher sends only turns whose delivery disposition says the user received the answer
  ([ADR-063](adr-063-record-delivery-disposition-for-released-turns.md)) and whose family is
  enabled. Every other outbox row, including one whose family was disabled after admission, ends in
  a terminal not-dispatched state and receives the outbox's delivered-row expiry. Released content
  is never kept indefinitely, and a later re-enable never sends a stale row.
- While evaluation is disabled, released content still waits in the outbox until that expiry. That
  bounded custody is the cost of keeping the switch out of the route.
- Every stored verdict carries the evaluator's revision, model identifier, and prompt digest, which
  the ingress takes from the turn's delivery receipt.

### 5. The regression path

- **Evaluators.** Each ships with a fictional calibration corpus
  ([evaluation validity steering](../../.kiro/steering/agent-evaluation-validity.md)). A calibration
  runner scores the corpus through the on-demand `Evaluate` API, which accepts a custom evaluator and
  returns value, label, and explanation [verified 2026-09-26, AgentCore `Evaluate` API reference].
  A failed expected outcome blocks the evaluator, and the gate's step 5 runs it before enabling, so a
  direct deploy cannot skip it.
- **The agent.** A versioned fictional question set runs through the deployed dev conversation route
  before and after a prompt, contract, tool, or model change. The released evaluators score each
  answer through the same runner, and the change records expected-versus-actual outcomes.
- That runner, in both modes, plus the live acceptance probes, is the regression path ADR-059 names.
  The versioned endpoint harness it once named is retired
  ([ADR-057](adr-057-evaluate-endpoint-behavior-with-a-versioned-scenario-harness.md)).
- Whether `Evaluate` accepts this repository's released-response span shape is [unverified]; batch
  evaluation over the input group is the fallback [snippet]. If neither accepts it, the gate cannot
  calibrate, so evaluation stays disabled and the gap is recorded for the owner; an evaluator is
  never enabled on absent calibration evidence.
- Score metrics per evaluator feed a dashboard and alarms. The review API's `turns` route
  ([ADR-078](adr-078-serve-quality-review-to-programmatic-callers-through-the-cloudfront-edge.md))
  is the list surface; there is no separate worst-results endpoint.

### 6. Retired in the same slices

- The CloudFormation configurations; the enabled, retention-confirmation, and capture flags and the
  validator branches that cross-check them; the service-owned result group name helper; the
  deploy-time configuration-identifier list in the ingress; and the outputs that print them.
- The result-group and configuration-identifier SSM parameters the evaluation stacks produce today.
  The owned group names get new parameter keys produced by the evidence stack. Following the rename
  rule, the new keys land first, their readers redeploy, and the old keys are removed in a later
  deploy of the same slice. *(Superseded 2026-09-27: the rename took one deploy pass; see the
  amendment above.)*
- Once the owned groups are live, the service-created result groups themselves, under the owner's
  deletion approval of 2026-09-26.
- The new deploy order places the result-link stack after the evidence stack and before the two
  evaluation stacks; the typed deploy order is changed in the same slice.

## Consequences

- Evaluation cannot be on while capture is off, cannot run onto a group whose controls were never
  read back, and cannot be enabled with an evaluator that fails its calibration.
- A second custom resource joins the Harness provider. It must be idempotent under CloudFormation
  retries and must never answer success before its readback.
- Three behaviours are [unverified] until the gate's first live run: whether the evaluation service
  writes to a customer group under a customer-managed key, whether it needs a role grant to do so,
  and whether subscription delivery works from a key-encrypted group *(superseded 2026-09-27: with
  no key, what remains unverified is stated in the Agent Quality Loop design's risks)*. Each is a gate assertion; a
  failure leaves evaluation disabled.
- Verdicts linked before this change stay where they are and are not read.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Keep the CloudFormation resource and patch `outputConfig` from a custom resource beside it | Two owners of one configuration; a later stack update could reset the binding. |
| Fall back to the service-created group when the owned one is refused | Runs evaluation onto a group no stack owns, under a steering exception whose condition does not hold. |
| One switch for all families | Two gates in two stacks would both write it, and the last deploy would win. |
| A capture switch in the route | Request-serving settings are resolved once per container, so warm containers would miss an enable or a disable; the dispatcher reads on every run. |
| Provenance from the current release record | Results arrive late by design, so a result would be stamped with a revision it was not produced under, or refused after a disable. |
| A pre-deploy check that compares shell flags with deployed state | Still two sources of truth, and a direct `cdk deploy` escapes it. |
| Capture and dispatch whenever the plane is provisioned, with no switch anywhere | Released content would enter the input group with nothing evaluating it. |
| No key on the result group | *Superseded 2026-09-27: this is the decision now; see the amendment above.* A Logs resource policy cannot bound readers, so anyone with account-wide Logs read could read raw results. |

## Review trigger

Revisit when CloudFormation can declare the output destination, when the service's handling of
customer groups or keys changes, or when an evaluation family is added that needs a different
reader.
