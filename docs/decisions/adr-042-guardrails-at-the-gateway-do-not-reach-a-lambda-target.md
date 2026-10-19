# ADR-042: Guardrails at the gateway do not reach a Lambda target

Date: 2026-08-24
Status: Accepted, with numerical-grounding implementation superseded by [ADR-049](adr-049-make-validation-and-grounded-narration-part-of-publication-accuracy.md) and conversational publication superseded by [ADR-056](adr-056-publish-conversation-as-evidence-bound-semantic-blocks.md); amended 2026-09-29 by [ADR-083](adr-083-stream-the-agent-answer-and-govern-it-by-audit-and-evaluation.md): no outbound Guardrail and no streaming hold (deployment state in ADR-083's status line)

> **Implementation amendment, 2026-08-24.** The negative Gateway and Harness-stream probes below
> remain authoritative: AgentCore Policy still cannot inspect Lambda-target content, and F-09 remains
> a separate open inspection gap. They no longer imply that numerical grounding is unavailable.
> ADR-049 closes numerical grounding through nonce-bound stored evidence and strong route reads.
> ADR-056 retains that boundary while replacing exact rendering selection with evidence-bound semantic
> blocks and flexible, veto-assessed reasoning. Neither decision projects target content into Gateway
> policy context.

**Amends [ADR-041](adr-041-guardrails-are-policy-at-the-gateway-and-applyguardrail-in-the-route.md).**
That decision placed layer 2 as Bedrock Guardrails evaluated by a policy engine on the tool gateway,
and layer 4 as a contextual grounding check in the conversation route. It named the two probes that
would confirm each. Both were run against deployed `dev` and both came back negative, for reasons
neither the documentation nor the CDK surface exposes.

ADR-041's four-layer model still stands, as does its judgement about where each control belongs. What
changes is what two of them can actually inspect.

Every claim carries a marker. `[verified]` means a probe against `dev` or a fetched vendor page, and
the probe or page is named. `[unverified]` means it could not be settled and is recorded as open
rather than assumed.

## Context

### Probe A — the engine evaluates our gateway, and cannot inspect anything crossing it

ADR-041 asked whether a policy engine evaluates a **Lambda** gateway target, given that everything
documented about guardrails-at-the-gateway names Runtime and HTTP passthrough targets. The answer is
in two halves and they point in opposite directions.

**The engine works.** A `CfnPolicyEngine` attached to the existing MCP gateway through
`policyEngineConfiguration` reaches `ACTIVE`, a base permit policy reaches `CREATE_COMPLETE`, and a
real authenticated turn calls `measurement_metric_by_grain` through the gateway and returns figures
with the engine evaluating in `LOG_ONLY` `[verified 2026-08-24 against Adlytics-Agent-dev]`. So
deterministic Cedar authorization at the gateway is available to us.

**No guardrail can be written against it.** A `when guardrails` block requires a
`context.{input|output}.<field>` data path naming the value to extract, and a target action on our
gateway declares no context fields at all:

| Data path tried | Result |
|---|---|
| `context.output.text` | `is not present in the context of action` |
| `context.output.narrative` | `is not present in the context of action` |
| `context.input.metric_name` | `is not present in the context of action` |
| `context.input.filters` | `is not present in the context of action` |
| `context.input.not_a_field` (deliberately bogus) | `is not present in the context of action` |

`[verified 2026-08-24, `create_policy` against the deployed engine, statuses read from
`get_policy`]`. A declared field and an invented one fail identically, which is what makes this a
finding about the context rather than about the field names.

Two things were ruled out before concluding:

- **The action identifier.** It is the **target name** — `catalog` or `measurement`. The engine says
  so itself: *"Target 'X' does not exist in gateway '...'. Available targets: catalog, measurement"*.
  The documented `<TargetName>___POST:/invocations` form is rejected as an unrecognized action, and
  so are `POST:/mcp`, `tools/call` and a tool name. So the statements above were scoped to a
  recognized action, not to a mistyped one `[verified 2026-08-24]`.
- **A missing output schema.** `ToolDefinition` accepts an `outputSchema` and ours does not supply
  one, which was the obvious explanation. One was declared, deployed, and re-probed: the context is
  still empty `[verified 2026-08-24]`.

**The likely mechanism**, and it is inference rather than a finding: a Runtime or HTTP target carries
an **API schema** — `RuntimeTargetConfigurationProperty` has a `schema` property — from which
request and response bodies, and therefore context fields, can be derived. An
`McpLambdaTargetConfiguration` carries a *tool* schema, and that is evidently not projected into the
policy context. This explains why AWS documents guardrails against exactly the two target types that
have an API schema. Whether any configuration changes it is `[unverified]`.

**The evaluations are also not observable.** AWS's calibration guidance is to run traffic through
`LOG_ONLY` and build a confusion matrix from the confidence scores in the logs. No policy-engine log
group exists in the account, and the harness log group carries no policy, guardrail or authorization
entry after a real turn `[verified 2026-08-24]`. Whether a log delivery has to be configured is
`[unverified]`. So even where a guardrail were expressible, the loop AWS prescribes for choosing its
threshold is not currently available.

### Probe D — the tool envelope never reaches the conversation route

This probe is not in ADR-041. It was added because reading the route showed layer 4 had no obvious
grounding source, and it turned out to be as decisive as probe A.

ADR-041's layer 4 grounds the narration against tool output, and
[Conversational Runtime](../capabilities/conversational-runtime/README.md) then described the envelope's `Metadata` travelling on its own
channel so the model cannot paraphrase it. `AgentCoreHarness._events_in` reads that channel —
`contentBlockDelta.delta.toolResultMetadata` — and turns it into a `METADATA` event.

**It never arrives.** A real turn in which `measurement_metric_by_grain` ran and returned figures
produced exactly three kinds of SSE event: `text` (75), `tool_call` (1), `done` (1). No `metadata`
event, in a raw capture of the stream rather than through the probe's own classifier
`[verified 2026-08-24]`.

Two consequences, and the second is worse than the first:

**At the time of the 2026-08-24 probe, layer 4 had no grounding source on the Harness event
stream.** The route held the model's narration and received no tool output through that channel. The
negative transport finding remains valid; ADR-049 later supplied the source through nonce-bound
stored evidence.
- **The qualifying metadata reaches users only as the model's paraphrase.** Coverage, freshness and
  suppression are what make a figure honest, and the design puts them on a separate channel for
  exactly that reason. That channel is empty. In the probe turn the caveat about which days actually
  loaded was prose the model composed. That is a live gap, recorded as
  [G-48](../decision-coverage-registry.md#gap-register-g-nn), and it is adjacent to
  [G-44](../decision-coverage-registry.md#gap-register-g-nn): both are the model being the only narrator of something it
  should be constrained by.

## Decision

**At adoption, layer 2 shipped as deterministic Cedar authorization, inert, behind a flag that was
off. Layer 4 shipped as a narration content check rather than a grounding check.** The 2026-08-24
probe established that the Harness event stream supplied no tool output; ADR-049 later supplied
nonce-bound stored evidence and deterministic release without changing this Gateway result.

| Layer | ADR-041 said | What shipped at adoption | Why |
|---|---|---|---|
| 1 · prompt filtering | `ApplyGuardrail`, `source=INPUT` | unchanged | reachable in our own route |
| 2 · tool I/O | guardrails at the gateway | policy engine, base permit only | no guardrail can be scoped to the target |
| 3 · in-tool validation | domain code | unchanged | already built |
| 4 · grounding | `ApplyGuardrail` grounding on tool output | `ApplyGuardrail` content and PII on the narration | no grounding source exists in the route |

Four sub-decisions.

**The policy engine ships even though it inspects nothing.** It is the prerequisite for any Cedar
policy, it establishes the IAM, the escape hatch, the region gate and the naming, and it makes a
promotion to `ENFORCE` a configuration change rather than a project. It ships **off**, and not only
because of region support: every tool call costs an `AuthorizeAction` evaluation, so while the only
statement is a blanket permit the engine is latency and spend on the critical path for no benefit.
Enabling it is a deliberate act, as `ADLYTICS_TRANSACTION_SEARCH` is.

**Cedar generation is not built.** ADR-041 decided the statements are generated from a declarative
source, for the same reason the Iceberg DDL and the tool schemas are. That reasoning is sound and the
condition for it has not arrived: one blanket permit is expressible, and a generator, a contract
file, two `make` targets and a validate-on-read loader around a single statement is machinery around
nothing. **This reopens when there is a second statement to generate** — which is the honest trigger
rather than a deferral with no end.

**At adoption, layer 4 became a content control and partly covered layer 2's loss.**
`ApplyGuardrail` with `source=OUTPUT` evaluated content filters and sensitive-information detection on
the narration without a grounding source. That was not the control ADR-041 specified and could not
catch an invented figure. ADR-049 later added the separate deterministic numerical-release boundary;
the content control still catches PII repeated into the answer, not PII present only in a tool result.

**The streaming hold was kept even before grounding existed.** ADR-041 held the final block so a
figure could not be read before assessment. That reasoning also applied to the content check: text
that the `OUTPUT` filter rejected could not already be on the reader's screen. ADR-049 now uses the
same hold for exact selection validation and deterministic release before the content check.

## Consequences

**Layer 2's own justification no longer holds, and §7's rating changes.** §7 rates layer 2 the
strongest control because it executes outside agent-influenced code and sees tool return data. The
first half is still true and the second is not: it sees no data at all. Layer 3 — deterministic
domain code building the envelope — is now the only control on tool output, and layer 4 is the only
control on what the model says about it. §7 is corrected with this change.

**F-09 is not closed.** ADR-041 recorded that gateway-side evaluation closes the hole F-09 named —
Guardrails PII filters not inspecting `tool_use` output. It does not. F-09 stays open with a narrower
description: the inspection point that would have closed it is unavailable for this target type.

**The Gateway remedies remain relevant only to F-09.** A Gateway response interceptor Lambda is
`InterceptionPoint.RESPONSE` on the L2 and is deterministic code at a managed enforcement point that
sees the tool result. Converting Lambda targets to HTTP targets with declared request and response
schemas could also produce a policy context, at the cost of changing the settled tool surface. ADR-049
removed either remedy as a prerequisite for numerical grounding; choosing one for raw target-content
inspection remains a separate decision.

**An unmeasured cost is now measured.** ADR-041 recorded two extra synchronous Bedrock calls per turn
as `[unverified]`. A quick turn with no guardrail calls completes in 11.8–12.2 s with first text at
4.1–4.4 s `[verified 2026-08-24, three runs]`. That is the baseline the layer 1 and layer 4 calls are
measured against once the route carries them.

**Ten constraints were found by deploy failure rather than by documentation**, and each is now encoded
in code with a test. They are recorded in
[finding register](../decision-coverage-registry.md#finding-register-f-nn) F-22 rather than here, because they are
facts about the service surface rather than parts of this decision. The two that would cost another engineer the most time: `definition.cedar` and
`definition.policy` are different languages rather than one property under two names, which ADR-041
recorded incorrectly; and the gateway must carry an explicit dependency on its own role's policy or
the update races and fails with the grant plainly present in the template.

## Alternatives considered

**Keep the guardrail policy in the code and let it fail.** Rejected. It cannot stabilise, so it is not
a deferred feature — it is a resource that fails every deploy. A test now asserts no `when guardrails`
statement is rendered, so the negative finding cannot be quietly reversed by someone who reads
ADR-041 and not this.

**Ship no policy engine at all.** Genuinely arguable, and it was close. An inert control is exactly
what this repository warns against, and the engine adds a per-request evaluation for no present
benefit. It ships because it is off by default, because the work that surrounds it — the region gate,
the IAM set, the escape hatch, the naming — is the expensive part and is now verified, and because
`ENFORCE` with a scoped permit is a real control that is one commit away. Had the flag defaulted on,
this would have gone the other way.

**Scope the base permit to the two known target actions, so a new target fails closed.** Deferred,
and this one is worth revisiting first. It would make an unreviewed gateway target denied by default
under `ENFORCE`, which is the right direction. It is not done because the full action set is not
observable: if a management or discovery action exists that the two target names do not cover, a
scoped permit would deny it, and `list_policy_summaries` returns nothing even for a live policy so the
engine cannot be interrogated for what it knows. Guessing the action set and shipping default-deny on
the guess is the trade this repository does not make.

**Have the tool write its envelope somewhere the route can read.** Rejected as the wrong shape for
*this* decision because it added a store, a write on the tool path and a read on the route path while
ADR-042 was recording Gateway behavior. ADR-049 later weighed and implemented the nonce-bound stored
form because deployed grounding evidence justified that coupling. The rejection remains the history
of scope, not a present prohibition.

## Review trigger

Reopen when any of these changes:

- A gateway target type we use gains a policy context — a Lambda target projecting its tool schema, or
  an `outputSchema` that reaches `context.output.*`. Layer 2's content half becomes available again.
- `toolResultMetadata` starts arriving on the harness stream. G-48 is already closed by ADR-049;
  this would remove the stored-evidence hop rather than create the first grounding source.
- A second Cedar statement becomes expressible. That is the trigger for the generator ADR-041 called
  for.
- Policy-engine evaluations become observable. Without them no threshold can be calibrated, so this
  gates any promotion to `ENFORCE` as much as it gates guardrails.
