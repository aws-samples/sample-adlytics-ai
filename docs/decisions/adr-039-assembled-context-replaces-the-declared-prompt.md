# ADR-039: An assembled context replaces the declared prompt, so the invariant travels with it

Date: 2026-08-23
Status: Accepted
Settles the transport question left by the former context-assembly design: *how* assembled
context reaches the model. Does not amend [ADR-038](adr-038-the-conversation-route-streams-from-a-function-url.md)
— every property of that transport is preserved. Does not amend
[ADR-006](adr-006-agentcore-agentic-layer.md) or [ADR-007](adr-007-auth-cookie-bff.md): what a caller may set is
unchanged, and this decision is about where a *server-side* value travels.

## Context

The former context assembly built per-turn context. Nothing in the design said how that context reaches the
model, and there is exactly one channel available.

**`systemPrompt` is the only one.** `InvokeHarness` takes `messages` with a
`HarnessConversationRole` of `user` or `assistant` — there is no `system` role to put a system message
in — so per-turn instructions have to arrive as the `systemPrompt` per-invocation override or not at
all.

**Whether that override merges or replaces was undocumented, and the answer decides the whole design.**
`harness-models.html` says "Override any of those on a single invocation … only that call uses the
overrides", which is about the *resource* not being mutated rather than about merge semantics. The API
reference uses one identical sentence for `model`, `maxTokens`, `timeoutSeconds`, `allowedTools` and
`systemPrompt`: "If specified, overrides the harness default." For every scalar in that list
replacement is the only coherent reading, so the wording carries no information about the one
list-valued field where it is genuinely ambiguous.

**Measured instead.** [verified 2026-08-23, against the deployed dev harness]

| Run | Override sent | Answer to "What is 17 multiplied by 23?" | Peak tokens |
|---|---|---|---|
| control | none | refused the arithmetic, obeying the declared prompt | 4,815 |
| override | only *"begin every reply with BANANA"* | `BANANA` then `391` | 4,582 |

**It replaces.** The declared prompt was not merely deprioritised — the arithmetic refusal it exists to
produce was gone, and the token count fell by the size of the text that stopped being sent. Reproduce
with `validation/probe_harness.py --system-prompt "..." --question "What is 17 multiplied by 23?"`.

That has a consequence the design had not accounted for: **the moment context is assembled, the
tool-contract invariant stops being a property of the deployed resource.** [Conversational Runtime](../capabilities/conversational-runtime/README.md)
§4 makes "the tool rounds, scales and labels; the model never divides, sums or converts"
non-negotiable, and until now it was held by `systemPrompt` on `CfnHarness`. An assembled prompt that
omitted it would produce a model doing arithmetic on figures, in production.

**And the test that looks like it guards this does not.** `infra/test/harness-stack.test.ts` asserts the
substring against the *resource block*. It stays green while the runtime behaviour changes, because the
resource still declares a prompt that is no longer the one being used. That is the most dangerous shape
a test can have: it reports a property it has stopped covering.

## Decision

**The assembled context carries the invariant text itself, read from one file, and it cannot be
constructed without it.**

Four parts, each closing one way the above could go wrong.

**One file, two readers.** `contracts/shared/agent-base-prompt.md` holds the text.
`infra/lib/stacks/harness-stack.ts` reads it at synth and sets it as the resource's `systemPrompt`;
`adapters/inbound/conversation/assembly.py` reads it at runtime as the first layer of the assembled
prompt. Not a constant in each: two copies drift, and the drift is silent in the dangerous direction —
the resource block would still assert the invariant while the string every turn actually sends had lost
it. That directory was already the right home, being bundled by `build-lambdas` and already read at
synth for `contracts/generated/tools.json`.

**The invariant is a construction precondition, not an assertion.** `AssembledContext.__post_init__`
refuses a `base` that does not contain `must come from a tool result`, so a prompt without it is
**unconstructible**. The same substring is checked when `harness-stack.ts` reads the file, so an edit
that removed the clause fails the synth as well as the type. Three checks, one phrase.

**The resource keeps declaring a prompt, and that is now a designed degraded path rather than a
leftover.** Because the override replaces, *omitting* it leaves the declared prompt in force — so a
turn whose context could not be assembled answers under the full invariant and loses only the context.
`AgentCoreHarness` therefore treats an empty context as "send no `systemPrompt` key", and
`ContextAssembly.for_turn` returns an empty string rather than raising for every failure it knows
about. The failure mode of the context engine is a turn with less context, never a turn with fewer
constraints.

**Context travels as a constructor argument, never on `TurnRequest`.**
`ports/outbound/conversation` promises that a turn request has **no field** for any per-invocation
override, so that the API layer *constructs* an invocation and never forwards one. A `system_prompt`
field on `TurnRequest` would demote that from a structural property to a convention, and
`FORBIDDEN_OVERRIDE_FIELDS` would be forbidding a name the domain object carried. So the value travels
`server.py` → `ConversationTooling.conversation_for(bearer, context)` → `HarnessInvocation` →
`_payload`. The port signature and the forbidden-field list are untouched, and a browser still cannot
reach it.

## Consequences

**A prompt edit is now a Lambda artifact rebuild.** `HarnessStack` was split out of `AgentStack` on
rate of change, precisely so the highest-churn artefact could be deployed without a changeset over two
tool functions and a 30 MB layer. That argument is weaker now: the text lives in `contracts/`, which the
Lambda bundle carries, so editing it means `make build-lambdas` and a redeploy of `ApiStack` as well.
The stack split still buys independent deployment; the churn it was isolating has moved rather than
gone. Recorded in `bin/app.ts` beside the stack it affects.

**The prompt is billed on every model request of every iteration**, not once per turn. That makes an
unbounded context an unbounded per-turn cost, arriving through a door
[G-04](../decision-coverage-registry.md#g-04--no-waf-rate-limiting-or-per-user-usage-admission)'s `maxTokens` budget does not
cover — `maxTokens` bounds generation. Hence `MAX_CONTEXT_CHARS`, and hence a breach raising rather than
truncating: the layers are ordered by priority with the invariant first, so a truncation that fit would
cut the capability statement and one that did not would cut the invariant. §4's deterministic drop order
is what turns a breach into a decision, and it is the increment after this one.

**The infrastructure test is kept and reinterpreted rather than deleted.** It no longer guards the
runtime invariant — nothing about a resource property can — but it does guard the degraded path, which
is the only thing that prompt is now for. Its comment says so, so the next reader does not mistake its
scope again. The runtime guard is `tests/unit/test_context_engine.py`, and the end-to-end guard is the
arithmetic refusal on a real turn: the §Context experiment handed us a regression test, because a
context that lost the invariant answers `391`.

**One inline DynamoDB statement lands on the conversation function.** The capability layer reads the
freshness registry once per turn. `ADL_FRESHNESS_TABLE` was already set on that function with no grant
behind it — correct, because withholding a string is not an authorization mechanism — and the grant is
`GetItem` and `Scan`, read-only, scoped to the freshness table alone. The run ledger stays unreachable
by resource ARN rather than by a condition expression.

**Two identity properties are unchanged and worth stating, because a reviewer will ask.** The context
is server-assembled and no part of it comes from a request body, so `_refuse_overrides` still rejects a
caller who mentions `systemPrompt` and the rejection is still meaningful. And `actorId` remains the only
identity value crossing into an invocation.

## Alternatives rejected

**Append the context as a leading `user` message.** Survives the replace semantics entirely, because it
touches no override. Rejected because the harness injects conversation history itself [verified
2026-08-23], so an injected turn joins the transcript: it would be summarised into long-term memory,
retrieved on later turns, and become indistinguishable from something the customer said. That is
[G-44](../decision-coverage-registry.md#g-44--a-corrected-registry-does-not-correct-what-the-agent-already-remembers)
made worse by design — the mechanism whose whole purpose is to state the current truth would become the
mechanism that preserves a stale one.

**Two `systemPrompt` blocks: the declared text plus the context.** Reads as a merge and would let the
resource keep owning the invariant. Rejected because the override replaces the *whole* field, so both
blocks would have to be sent every turn — which is the same amount of text through a shape that
*implies* the resource contributed one of them. A future reader would take the two-block form as
evidence of merging, which is the belief this ADR exists to correct.

**Redeploy the harness resource per turn.** Absurd at the rate a turn happens, and it would make context
a deploy artefact. Named only because "override" documentation talks about not mutating the resource,
which invites the reading that mutation is the alternative.

**Leave the invariant on the resource and accept the risk.** Rejected on the measurement: it is not a
risk, it is what the measured behaviour does. A deployment doing this answers `391`.

## Review trigger

Any of these reopens it:

- AWS documents merge semantics for `systemPrompt`, or the measured behaviour changes. Re-run
  `probe_harness.py` with the control and the override; the arithmetic refusal is the signal.
- A `system` conversation role appears on `HarnessConversationRole`, which would give per-turn
  instructions a channel that is not an override.
- The assembled context approaches `MAX_CONTEXT_CHARS` in a real deployment, which means the drop order
  is load-bearing rather than a safeguard and its own decision is due.
- Anything other than the conversation route needs to assemble context — a scheduled digest, say — since
  the base text would then have a third reader and the file's format becomes an interface.
