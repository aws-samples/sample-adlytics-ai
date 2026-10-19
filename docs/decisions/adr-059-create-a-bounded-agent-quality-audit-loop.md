# ADR-059 — Create a bounded Agent Quality audit loop

Date: 2026-09-02
Status: Accepted — implemented and dev accepted; the audit-before-release receipt is written at plan acceptance since ADR-065, unchanged in rule; amended 2026-09-26 by ADR-079, ADR-080, and ADR-081 (current state in each one's status line); amended 2026-09-29 by [ADR-083](adr-083-stream-the-agent-answer-and-govern-it-by-audit-and-evaluation.md): the receipt is written after the streamed answer and before `done`, not before release, and only complete turns are admitted to evaluation (deployment state in ADR-083's status line)

> **Amended 2026-09-26 — the regression path (evaluator mode implemented in the repository
> 2026-09-27; agent mode implemented in the repository 2026-09-27).** The "versioned
> endpoint harness" in the Decision below was retired with
> [ADR-057](adr-057-evaluate-endpoint-behavior-with-a-versioned-scenario-harness.md). The regression
> path for a prompt, contract, tool, or model change is the runner of
> [ADR-079](adr-079-enable-online-evaluation-only-through-a-verifying-gate.md) §5, which calibrates
> evaluators and runs a fictional question set through the agent before and after the change, plus
> the live acceptance probes. Its expected-versus-actual outcomes are recorded with the change. Until
> the runner exists, a change of that kind records the live probes' outcomes and names the runner's
> absence.

> **Amended 2026-09-26 — gated evaluation onto owned results (implemented in the repository
> 2026-09-27; current state in ADR-079's status line).**
> [ADR-079](adr-079-enable-online-evaluation-only-through-a-verifying-gate.md) will replace the
> "exact service-owned group" below with a stack-owned result group per evaluation family (its
> key was withdrawn 2026-09-27), bound and verified by a custom resource before evaluation is enabled, and make the outbox
> dispatcher decide what reaches evaluation from the release record that custom resource
> publishes.

> **Amended 2026-09-26 — one writer per record family (partly implemented; current state in ADR-080's status line).**
> [ADR-080](adr-080-confine-each-audit-and-evaluation-record-family-to-one-writer.md) will split the
> retained storage boundary below into one table per writer, each with a resource policy that admits
> only that writer and denies modification and export to everyone.

> **Amended 2026-09-26 — a support reference, full terminal coverage, and retention (decided, not
> yet implemented).** [ADR-081](adr-081-make-every-turn-traceable-and-attributable.md) reverses "no
> public audit reference": the chat stream's terminal event will carry the `audit_id`. It will also
> make the receipt write run on every exit path, and give every identifiable record a declared
> retention.

> **Amended 2026-09-02 — event-driven protected evaluator results.** A service-owned evaluation
> result may arrive after an arbitrary delay, so the prior bounded query/retry closure is not an evaluator
> outcome. The exact AgentCore result group now has one source-scoped CloudWatch Logs subscription to a
> dedicated ingress Lambda. Its gzip/base64 envelope is decoded only in memory under explicit size limits;
> decoded values are never logged, persisted, queued, traced, metered, or sent to EventBridge. The ingress
> accepts only the configured evaluator, uses the service trace plus managed `session.id` to strongly
> re-attest one audit/writer pair, then emits canonical content-free `EvaluationResult.v1` bytes to an
encrypted queue. A separate persister conditionally writes only that projection. There is no synthetic
> `unavailable` companion: absent delivery remains an acceptance failure/pending observation. This is a
> deliberate protected-content boundary change and replaces the prior Logs Insights polling path.

> **Refined 2026-09-10 — delivery disposition.** The receipt is persisted before public blocks are
> streamed, so a persisted release does not prove the user received it.
> [ADR-063](adr-063-record-delivery-disposition-for-released-turns.md) records a bounded post-stream
> delivery disposition that evaluation analysis joins; the persist-before-release ordering is unchanged.

> **Refined 2026-09-14 — V3 receipt, anchored trajectory, one result per evaluator.**
> [ADR-064](adr-064-retire-planner-era-contracts-and-anchor-the-audit-trajectory.md) retires the
> planner-era receipt fields, records the turn trace ID and every tool call's trace anchor and
> plan selection on the receipt, anchors findings to trajectory steps, and keys evaluation results
> by admission and evaluator so the V3 judge and the V4 dimensions link side by side. The ingress
> now accepts a record by its released online-evaluation configuration ID rather than one
> configured evaluator name.

## Context

The conversation Runtime currently records several valuable but disconnected planes: transient
turn-evidence state keyed by turn, immutable exact prompt receipts, durable released epistemic
reasoning, correlation-scoped logs/spans, and an optional protected exact question/answer evaluation
source. No durable record links one terminal turn's language-to-objective proposal, server admission,
context use, authority selection, publication outcome, and evaluation result.

A generic terminal such as unavailable operation can therefore hide materially different causes. The
browser reproduction of a new-user exploration message established the practical consequence: it is
possible to observe a refusal without a retrievable answer to whether planning omitted orientation,
admission rejected a capability, page context was over-applied, or a model/runtime dependency failed.

CloudWatch application telemetry cannot become that record. It deliberately excludes question, answer, identity,
session, page reference, prompt, model output, tool arguments, evidence values, and result payloads. Managed
Harness diagnostics may retain detailed execution payloads in a separate protected customer-account plane, but
that plane is neither an immutable audit contract nor a released-response quality source. The transient
turn-evidence table is destroyable/TTL-bound execution state, while prompt and evaluation planes may contain
sensitive content. A product-quality loop needs durable bounded audit data, protected semantic evaluation,
explicit retrieval paths, and versioned synthetic regression scenarios—not a browser thumbs-up control or
uncontrolled prompt adaptation.

## Decision

**Create a customer-account Agent Quality Loop that records one bounded immutable audit receipt for
every terminal turn, processes it asynchronously, links selected receipts to protected evaluation, and
uses human-reviewed findings to drive versioned synthetic regression scenarios.**

The Runtime creates `ConversationAuditRecord.v1` from server-observed decisions after it has a terminal
publication/refusal outcome. The receipt is not model-authored, is not public response content, and
contains no raw conversation or customer data. It contains only closed outcome/category values,
version/digest references, timestamps, opaque internal references, and selected bounded counts.

The record covers every terminal class: complete response, partial response, clarification, governed
refusal, input/output policy intervention, and dependency failure. It records proposed and admitted
objective categories separately, page-context availability/use as bounded states, authority source,
operation category/outcome, publication outcome per objective, terminal category, and exact
runtime/prompt/catalog/page-schema/response-contract versions. It stores references—not contents—to
turn evidence, prompt receipts, released epistemic records, and any evaluation admission. A
publication-refused completion may also retain one closed completion boundary; it never retains model
text, prompt text, exception detail, or a publisher reason.

A new retained Agent Quality storage boundary owns the audit receipt and derived audit findings. It is
separate from transient TurnEvidence, exact prompt evidence, and content-bearing evaluation transport.
It uses encryption, point-in-time recovery, conditional immutable writes, explicit retention policy,
and least-privilege roles. An exact `audit_id` is the primary retrieval key. A narrow correlation index
allows an authorized operator to map one normalized correlation to one or more audit records; aggregate
indexes use time/version/bounded outcome dimensions. No table scan or browser access is an audit
retrieval interface.

A stream or queue delivers new audit IDs to an asynchronous processor. The processor verifies the
canonical record, derives deterministic structural findings, writes them immutably against the audit
record, and emits only bounded aggregate measurements. It may not reconstruct or log raw conversation
content. Trace-write failure never authorizes an unsafe response and must not turn an otherwise valid
published answer into a failure; the Runtime reports the missing audit receipt as a bounded dependency
signal and the processor measures the resulting audit-coverage gap.

Selected turns may enter the existing protected released-response evaluation flow. The evaluation
envelope gains `audit_id` so evaluator output can link back to the mapping/admission/publication receipt.
AgentCore publishes a result event to the exact service-owned group configured for the released evaluator.
One source-scoped CloudWatch Logs subscription invokes an isolated ingress Lambda. It holds the gzip/base64
envelope only in memory, validates bounded delivery/event fields and the configured evaluator, and never
logs, traces, queues, stores, meters, or dead-letters decoded protected material. The ingress combines the
managed `session.id` with the service trace only in memory, queries the narrow correlation index, and
strongly attests exactly one audit receipt and writer receipt. It then emits only canonical
`EvaluationResult.v1` bytes to an encrypted projection queue. A dedicated persister, with no Logs read
permission, conditionally writes the content-free companion. Exact question and answer remain solely in
the controlled evaluation source governed by [observability](../concepts/observability.md).
The audit receipt retains only evaluator version, admission state, and bounded evaluator outcomes. No
evaluator runs on the user-visible request path, and missing service delivery never becomes a synthetic
score, evaluator failure, or terminal availability state.

An internal operator retrieval adapter accepts one validated `audit_id` or normalized correlation ID
and returns a redacted audit projection. Privileged, exact-key follow-ups may resolve referenced
turn/prompt/epistemic/evaluation records under their existing authorization policies. It never exposes
raw content by default and never uses an owner/session identifier as a telemetry or aggregate dimension.
There is no browser feedback endpoint, feedback UI, or public audit reference in this decision.

Agent-quality changes remain human-reviewed. An audit pattern becomes a fictional scenario archetype,
not a copied production prompt. A prompt, contract, tool, or configuration change creates a new baseline
and must be evaluated through the versioned endpoint harness before non-production deployment. The
system never autonomously changes prompts, models, tool permissions, or publication policy from audit
records.

## Consequences

The platform gains a retrievable explanation of how an agent turn was mapped and governed without
turning telemetry into a transcript store. An operator can distinguish a planner proposal failure from
an admission refusal, an unavailable page objective from an irrelevant page context, and a valid
partial response from a whole-turn dependency failure.

The quality loop becomes explicit:

```text
terminal turn → immutable audit receipt → structural processing → protected evaluation,
when selected → bounded finding/aggregate → human review → fictional scenario →
versioned change → before/after evaluation
```

This adds a retained data boundary, asynchronous processing, retrieval authorization, retention policy,
operator tooling, and new versioned contracts. It must not be added to the existing transient Runtime
stack merely for implementation convenience: retained and destroyable resources have different failure
and deletion boundaries.

The audit record improves diagnosis but cannot alone judge semantic correctness because it excludes
exact language. Protected evaluation and controlled synthetic scenarios remain necessary. Conversely,
raw evaluation alone cannot explain which server admission/authority branch occurred; both surfaces are
required and linked by `audit_id`.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Use CloudWatch logs and traces as the quality database | Repository telemetry intentionally excludes the content and durable join fields required for audit; log retention and operator access are not the audit contract. |
| Extend transient TurnEvidence with long-lived quality records | Couples a retained product-quality record to a TTL/destroyable execution boundary and excludes turns that perform no evidence work. |
| Store exact question and answer in every audit receipt | Creates an unnecessary raw-transcript store and duplicates the separately governed evaluation source. |
| Add browser feedback first | User feedback is useful later but does not provide structural mapping evidence or an internal quality operating loop. |
| Let an evaluator automatically rewrite prompts from findings | Makes production traffic an unreviewed policy-authoring input and destroys reproducible baselines. |
| Review only aggregate metrics | Aggregates show a trend but cannot explain one decision path or produce an attributable regression scenario. |

## Review trigger

Revisit this decision when:

- retention, reader access, or legal policy requires a different audit-data lifecycle;
- audit persistence coverage cannot meet the accepted reliability target;
- protected evaluation cannot link an exact released pair to an audit record without widening content
  exposure;
- a multi-turn quality contract is accepted;
- aggregate patterns require a different storage/query architecture; or
- user-facing feedback is separately approved as a product capability.