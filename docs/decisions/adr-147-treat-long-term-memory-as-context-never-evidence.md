# ADR-147: Treat long-term memory as context, never evidence

Date: 2026-10-18

Status: Proposed (2026-10-18). The owner approved the scope on 2026-10-18: the semantic strategy
override with its execution role, the prompt rule, the evaluation cases, and a one-time audit of
existing records. Record expiry (decision 6) waits for a separate owner decision. Decisions 1 to 5
and 7 were deployed to dev and accepted live on 2026-10-18, with the amendment below
([dated result](../../validation/RESULTS.md#chat-long-term-memory-fix-adr-147-on-dev--2026-10-18)).

> **Amended 2026-10-19 (owner decisions of 2026-10-18 after live acceptance), decisions 1 and 5.**
> - **User-stated goals may stay in memory.** Long-term memory may keep a goal the user typed,
>   numeric target included, attributed to the user and never presented as a measured result.
>   Live acceptance found two such records: one under `advertiser_context`, whose extraction
>   prompt says goals are stored without a target value, and one under the unchanged user
>   preference strategy. The owner accepted both and kept them. This narrows decision 1's
>   "goals (without numeric targets)" and the audit rubric's "a numeric goal or budget" clause.
>   No record held a value a tool or the assistant returned.
> - **The live check of both failure shapes is the evidence for decision 5.** In live
>   acceptance, the two agent-regression questions went unscored: the evaluator graded their
>   seed answers below Grounded, so the seed check refused them. The owner accepted the live
>   turns in their place. Those turns covered both failure shapes, each for a fresh persona and
>   for the persona that showed the defect, and no answer stated a remembered figure. The two
>   questions stay in the set, unscored, until the seed grading is fixed (a
>   [known limit](../../README.md#known-limits)).

## Context

[ADR-006](adr-006-agentcore-agentic-layer.md) says AgentCore Memory "is not factual evidence or
analytical authority", and [ADR-082](adr-082-govern-the-agent-by-evaluation-and-audit-not-text-heuristics.md)
§1 calls a number from memory a fabrication. Dev chat acceptance on 2026-10-18 found both broken
for a customer:

- **New-to-brand customers.** The lake holds no buyer figure, so the tool refused. The answer said
  so, then added "based on your tracking records, you acquired 45,524 new-to-brand customers". That
  is an earlier conversation's new-to-brand *purchases* figure, mislabelled as customers.
- **Reach for an unlanded week.** The tool refused correctly. The answer then restated the previous
  week's six figures "based on your previous query", from a different conversation.

Grounded correctness labelled both turns Unsupported, and its alarm fired, so detection works. But
the customer still read the numbers.

Root cause [verified 2026-10-18, dev]:

- The Memory's managed semantic strategy, keyed by actor, stored those exact figures as "facts
  about the user". The Harness retrieves that namespace into every later conversation of the same
  user.
- The service's default semantic extraction prompt treats tool and JSON payloads as facts about the
  user. Its fixed output schema asks for "specific numbers" [verified 2026-10-18,
  [semantic system prompt](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-system-prompt.html)].
- The Harness prompt already forbids stating "a figure … from memory". On its own it did not hold.

## Decision

**Long-term memory may carry who the user is, what they work on, what they want and how they
talk. It never carries a measured result, and the agent never states a figure it only recalls.
This is governed by memory configuration, the agent prompt, evaluation, and audit. Nothing gates
answer text.**

1. **The semantic strategy becomes a built-in with overrides.**
   - The built-in semantic strategy gets a custom extraction and a custom consolidation prompt,
     both on one model.
   - Extraction keeps preferences, goals (without numeric targets), business context and
     vocabulary. It never stores a metric value, count, currency amount, percentage, rate, rank
     value, total or change, or any of these with a date or period, whoever stated it.
   - Consolidation skips any memory that carries one, and never adds or updates with one.
   - The output schema stays the service's.
   - The strategy gets a new name and namespace (`advertiser_context`, `/context/{actorId}`), so
     the old namespace leaves retrieval at cutover and is retired whole. Same-named in-place
     conversion is not documented.
   - The user preference and summary strategies are unchanged.
   - The prompts are contract files under `contracts/prompts/v1/`, read at synth.
2. **One override model, from the environment, with no fallback:** `ADLYTICS_MEMORY_MODEL_ID`.
   - The model chosen for dev is Claude Sonnet 4.6 through its US inference profile. It is
     `ACTIVE`, invocable in the account, and accepts `temperature`.
   - Sonnet 4.5 is `LEGACY`. Haiku 4.5 is being retired
     ([ADR-076](adr-076-tier-insight-narration-across-card-and-deep-dive-models.md)). Sonnet 5 and
     Opus 5 refuse `temperature` and native structured output, and the service's request shape is
     not documented [verified 2026-10-18].
   - If the service refuses the profile ID, the Memory stack update rolls back whole, with no
     partial state. The model is then the owner's decision. There is never an automatic fallback.
3. **The memory keeps the execution role the L2 created.**
   - Trust: the AgentCore service principal, under `aws:SourceAccount` and an `aws:SourceArn`
     limited to this memory's name.
   - The role gains invoke rights on that one inference profile, plus the profile's foundation
     model only through it (A-IAM-MODEL-001).
   - It never gets the AWS managed memory inference policy, which allows every model and profile
     in the account.
   - Those two statements are the role's only model grants. The override strategy is declared
     without the L2's own `bedrock:InvokeModel*` grant, and the tests pin the role's complete
     policy.
   - The memory resource depends on the role's policy, the ordering the L2's grant carried, so a
     create never leaves a retained memory whose role cannot invoke its override model.
4. **A strategy change refreshes the Harness.**
   - AWS requires `UpdateHarness` after a strategy change on an attached memory [verified
     2026-10-18,
     [Harness memory](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-memory.html)].
   - The Memory stack publishes a digest of its declared strategies. The Harness stack carries the
     digest as a custom-resource property, so a strategy change updates the Harness in the same
     deploy pass: one `make deploy-all-locked` run from a declared source
     ([ADR-136](adr-136-deploy-a-shared-asset-change-as-one-deploy-all-from-one-declared-source.md)),
     where the Memory stack precedes the Harness stack. The prompt rule (decision 5) and the
     strategy change ship from two separate commits, each in its own such run, so the prompt
     rule is tested live on its own first.
   - **If the Harness still retrieves the old namespace after that deploy:**
     - The rollout stops.
     - The audit runs at once, and deletion of every record in the retired namespace goes to the
       owner as containment.
     - If the deploy never sent `UpdateHarness`, the revision mechanism is fixed. If it sent
       `UpdateHarness` and retrieval stayed stale, the Harness spec declares its retrieval
       explicitly, resolved through `GetMemory`, as its own reviewed change.
     - A one-off operator `update-harness` is not used. It repeats the call that failed and
       leaves the fix outside the declaration.
     - The branch taken is recorded here.
5. **The agent prompt treats recalled context as context.**
   - It never states, repeats or rounds a figure found only in recalled context, or only in an
     earlier turn's tool result or answer. It never presents recall as the user's records.
   - A number the user typed in the conversation stays a premise. A number found only in recalled
     context is never a premise.
   - When a figure is needed, it re-reads with a governed tool. After a refusal, it offers the
     alternative the refusal names, as a fresh read.
   - Evaluation measures this. Two calibration cases are added, one per failure shape (a
     remembered figure where the metric has no data, and one after a refusal). Each expects
     exactly Unsupported, the label the worst-label alarm counts, unless the pre-merge stability
     check widens it to Partially Grounded, recorded as a known limit.
   - Two agent-regression questions are added. Each first seeds the figure in another
     conversation, with a seed that names no governed entity kind.
     - It scores the seed and confirms from the audit receipt that the seed read a measured
       result: a measurement tool step that succeeded.
     - It waits for the seed's record in the namespace the Harness retrieves.
     - It confirms the asked period is still unmeasured.
     - Any of these failing leaves the question unscored, never matched.
     - The existing questions run before any seed, so a seed never reaches their recall.
6. **Record expiry is proposed, not decided.** 90 days for every long-term strategy matches the
   event expiry and dev's receipt retention. CloudFormation cannot declare it today [verified
   2026-10-18], so implementation waits for the owner.
7. **Existing records are audited once.**
   - The operator agent classifies records in the cross-session namespaces by reading them
     against one written rubric, never by a pattern. The rubric uses the prompts' exemptions, and
     the date exemption in the same words: "a calendar date or period stated with no value".
   - The owner approves an exact deletion set, the operator deletes it, and every deletion is read
     back.

Decided by rubric items (1) correctness, (4) no dual paths and (3) simplicity
([delivery practice](../delivery-practice.md#5-the-decision-weighting-rubric)).

## Consequences

- A customer's answer can no longer draw a number from cross-session memory unless both controls
  fail: extraction would have to store a figure, and the agent would have to state it. Evaluation
  and the alarm still measure what gets through. Nothing blocks release, as under ADR-082.
- Cross-session context starts empty in every environment, because a new strategy does not
  backfill.
- Extraction and consolidation now bill the override model to the deploying account, and their
  failures are silent to chat. Acceptance proves records appear; memory log delivery is a later
  addition.
- Two additions keep the strategy and the Harness in lockstep: one more SSM parameter, and one more
  accepted property on the Harness custom resource.
- A follow-up that reuses an earlier turn's figure re-reads it, which may add one governed call.
- A calibration case that flips labels would disable evaluation through the fail-closed gate. Each
  new case is therefore stability-checked before merge.
- Conversation summaries, retrieved only within their own conversation, still hold figures. The
  prompt rule covers them.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Strengthen the prompt only | The prompt already forbade figures from memory and was broken twice in one run. |
| Remove the semantic strategy | Loses advertiser context (brands, goals, vocabulary). The owner chose to keep context and remove figures. |
| Self-managed strategy | Full control of the schema, at the cost of an extraction pipeline the repository would own. That breaks (3). |
| Convert in place under the old name and namespace | In-place conversion is undocumented, and figure-carrying records would stay in the retrieved namespace. That breaks (1) and (4). |
| Raise the semantic retrieval relevance threshold | Changes which records are injected, not what they contain. |
| Bedrock Guardrails contextual grounding, or a pattern check on answers | Gates model output, which ADR-082 forbids. Revisit only if the evaluation cases still show remembered figures after this change. |
| The AWS managed memory inference policy | Allows every foundation model and inference profile in the account. |

## Review trigger

Revisit when:

- the audit or evaluation finds a measured figure in a retrieved long-term namespace after
  cutover;
- the user preference or summary strategy is changed;
- CloudFormation gains record expiry;
- the override model's lifecycle leaves `ACTIVE`.
