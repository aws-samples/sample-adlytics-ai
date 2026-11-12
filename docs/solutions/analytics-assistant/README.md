# Governed analytics assistant

**Ask why a figure moved, in a sentence, and get an answer in which every number came from tested code, with a record of how it was answered.**

One of the [Adlytics AI solutions](../../../README.md#solutions). It deploys with the full sample, which is sample code and not production-ready: read [Cost](../../../README.md#cost) and [Security considerations](../../../README.md#security-considerations) first. The assistant runs beside every page. This solution covers the chat assistant, the governed tools it calls, the per-turn audit record, optional online evaluation, quality review and prompt administration.

## Who it's for

| Reader | What they do with it | Decision it supports |
|---|---|---|
| Everyone on the team: brand manager, media buyer, finance or CMO | Ask "why did it move?" next to the page they're reading | What to look at first |
| Analyst | Opens "How this was answered" to see which governed tools produced each figure | Signing off a figure before it goes to finance or a client |
| Operator or administrator | Reviews answer quality and manages the versioned prompts that write page insights | Whether answers stay trustworthy, and which prompt version runs |

Personas and every view's reader are in [who it is for](../../business-context.md#who-it-is-for).

## What you see

These images come from a development deployment with synthetic data, in the light theme, captured in October 2026. They replay a scripted answer, not a recorded conversation, so their campaign names don't appear in the other solutions' images. To regenerate them, see [`validation/capture_screenshots.mjs`](../../../validation/README.md#regenerating-readme-screenshots).

| ![Assistant panel on the Campaigns page mid-answer: two bullets about campaigns that moved ROAS, a collapsed How this was answered, and a progress list with completed tool calls and one in progress](../../images/chat-streamed-answer.png) | ![A finished assistant answer with three bullets and a note on ad-attributed purchases, How this was answered opened to three numbered steps, each a plain description with its governed tool name, and a Copy reference button](../../images/chat-answer-trace.png) | ![Assistant panel with the finished answer, its collapsed How this was answered and Copy reference, then a declined question with the refusal message and its own Copy reference](../../images/chat-refusal.png) |
|---|---|---|
| The answer arrives while it is written, and the reader sees each governed tool as it runs. The "Using this page" line names the page, channel and window the answer covers. | "How this was answered" lists each step in plain words with the governed tool that ran it, so an analyst can trace every figure. The answer notes that ad-attributed purchases won't match retail sales. | A question asking for individual shoppers is declined before it runs, with a suggestion of what to ask instead. "Copy reference" copies the turn's support reference. |

See [workspace assistant](../../ui/workspace-assistant.md) for the panel's behaviour.

## What it answers, and what it won't

It answers questions over your published data, using the page and window you're on:

- "Which campaigns did the most to move ROAS this week?"
- "What was my ROAS?"
- "Which campaigns spent the most this month?"
- "What metrics can I ask about?"

It refuses, and says why, when the data can't support an honest answer: anything about an individual shopper, retail sales credited to one campaign, competitor activity, or one merged revenue figure across Amazon Marketing Cloud (AMC) and Vendor Central. The full list, with the assistant's wording, is in [answerable questions](../../answerable-questions.md).

Current chat limits are rows in the hub's [Known limits](../../../README.md#known-limits): **Ask chat for reach by channel last quarter**, **Rely on chat never restating a remembered figure**, **Ask chat for Amazon DSP cost per click**, **Trust that a chat figure is for the channel you asked about**, **Get fast answers to hard chat questions** and **Trust every operations signal**.

## Data it uses

| Input | What it is | Freshness |
|---|---|---|
| Published data | AMC results your pipeline lands, read only through governed tools. Vendor Central reports can't be loaded into a customer deployment yet. | As fresh as the last publication; each answer names the window it covers. Sources: [AMC](../../sources/amc.md), [Vendor Central](../../sources/vendor-central.md). |
| Metric catalog | The declared metrics and their definitions, in [`contracts/shared/metric-dictionary.yaml`](../../../contracts/shared/metric-dictionary.yaml) | Changes only when you change the contract |
| Page context | The page and window you're on, sent as a reference the server redeems ([ADR-120](../../decisions/adr-120-carry-page-context-to-chat-as-a-server-redeemed-reference.md)) | Per question |
| Long-term memory | Per user, for context such as a stated goal; measured figures are kept out ([ADR-147](../../decisions/adr-147-treat-long-term-memory-as-context-never-evidence.md)) | Across conversations |

## How it works

**Call-flow diagram — how the assistant answers a question, and how its answers are checked.**

![Call-flow diagram: how the assistant answers a question, and how its answers are checked. A signed-in user asks a question in the browser. The Conversation API checks the sign-in, screens the question with Amazon Bedrock Guardrails and invokes the agent on Amazon Bedrock AgentCore as that user. The agent chooses governed tools and calls them through AgentCore Gateway; the tools compute every figure with Athena from the published data, or say why they cannot answer, and the answer streams back to the browser as it is written. The Conversation API writes one audit record per turn, with no question or answer text, before the answer ends. When the optional evaluation input is on, in any environment, it also writes each complete turn's exact question and released answer to the evaluation outbox. Optional online evaluation, off by default and accepted in any environment (live-accepted only in dev), scores a sample of those turns, 10% by default, keeps the scores in protected result log groups and raises an alarm when poor scores rise. Entitled operators read redacted turn records and scores through the protected quality review API.](diagrams/assistant-answer-path.svg)

> **Takeaway.** The model chooses tools and writes the explanation; tested tools compute every figure, and each turn leaves a record an operator can review.

- **Screened before it runs.** The Conversation API verifies the signed-in user and screens the question with Amazon Bedrock Guardrails. A screened-out question is declined without calling the agent.
- **The user's identity reaches every tool.** The agent runs as the signed-in user, and each tool verifies that identity.
- **Tools own the figures.** Measurement and catalog tools compute figures with Athena from the published version, and return them with any caveats (coverage, suppression, measurement basis), or a reason the question can't be answered.
- **Same data as the pages.** The tools read the governed lake and metric layer the [Platform](../platform/README.md) publishes, so chat and pages read the same published versions.

The runtime design is in [conversational runtime](../../capabilities/conversational-runtime/README.md).

## How it stays trustworthy

| Control | What it does | Owning doc |
|---|---|---|
| **No invented figures** | Every number comes from a governed tool. The agent states those figures and ones validly derived from them, such as a difference or a share, and never fabricates one. | [Business context](../../business-context.md#5-why-the-trust-posture-is-the-differentiator), [ADR-082](../../decisions/adr-082-govern-the-agent-by-evaluation-and-audit-not-text-heuristics.md) |
| **Audit record per turn** | Every turn, answered or refused, writes one record of the tools it used and its outcome, with no question or answer text. It is written before the answer ends, and kept for the days you set in `ADLYTICS_RECEIPT_RETENTION_DAYS`. | [Agent quality loop](../../capabilities/agent-quality-loop/README.md), [ADR-081](../../decisions/adr-081-make-every-turn-traceable-and-attributable.md) |
| **Online evaluation of a sample** | Optional and off by default. When it is on, it scores a sample of complete turns, 10% by default (`ADLYTICS_ONLINE_EVALUATION_SAMPLE_PERCENT`, read in [`infra/lib/config/index.ts`](../../../infra/lib/config/index.ts)), for grounded correctness, internal consistency and helpfulness. An aggregate alarm per evaluator fires when the share of poor verdicts rises; it shows a trend and never blocks a release. Answers outside the sample are not checked. `ADLYTICS_EVALUATION_INPUT_PROVISIONED` adds the evaluation outbox, and from then on the deployment keeps each complete turn's exact question and released answer there; synth accepts it in any environment. Scoring needs the outbox plus `ADLYTICS_ONLINE_EVALUATION_PROVISIONED`, `ADLYTICS_TRANSACTION_SEARCH` and `ADLYTICS_ONLINE_EVALUATION_V4_REQUESTED`, and synth accepts them in any environment (accepted in dev only: live acceptance has run only there). Without the last setting the gate leaves scoring disabled and no answer is scored ([evaluation enablement gate](../../capabilities/agent-quality-loop/low-level-design.md#evaluation-enablement-gate-adr-079)). The evaluation result log groups hold the same content as the outbox ([evaluation result custody](../../concepts/observability.md#37-evaluation-result-custody-adr-079)). | Hub [Optional features](../../../README.md#optional-features), [ADR-079](../../decisions/adr-079-enable-online-evaluation-only-through-a-verifying-gate.md) |
| **Quality review** | A protected API returns one turn's records, or a page of turns, to signed-in users who hold the review entitlement. It never returns question, answer or prompt text. A command-line client isn't built yet. | [Protected quality review API](../../capabilities/protected-quality-review-api/README.md), [ADR-060](../../decisions/adr-060-protect-quality-review-retrieval-with-an-explicit-operator-authorization-boundary.md) |
| **Prompt administration** | The prompts that write page insight cards are immutable versions. A version can be activated only after its calibration cases pass, and new versions record who created them. Only prompt administrators see the page. | [Prompt administration](../../ui/prompt-administration/README.md), [ADR-069](../../decisions/adr-069-version-insight-prompts-in-a-calibration-gated-registry.md) |

### Prompt administration

These two images are from 2026-10-19, so their sidebar predates the Overview item. The capture script regenerates them only when its signed-in user is in the prompt-admin group, which the synthetic capture user isn't. The first slot's versions are a fictional fixture, so no real author appears.

![Prompt administration page listing each insight prompt slot with its active version and other versions, one marked calibration needs changes and others calibration passed](../../images/prompt-administration.png)

An administrator sees every insight slot on one screen: its active prompt version, and whether each newer version passed calibration. In this image, the slots reading "No versions yet", such as Largest changes, are fixed-rule cards that never take a prompt. See [prompt administration](../../ui/prompt-administration/README.md).

![Two prompt versions: version 25 marked Active and created before authorship was recorded, and version 27 marked Needs changes, created by operator@example.com, failing 1 of 7 calibration cases](../../images/prompt-version-calibration.png)

Version 27 fails 1 of 7 calibration cases, so it reads "Needs changes" and can't be activated; version 25 stays active. Version 27 records its author, the fictional `operator@example.com`; version 25 predates authorship recording and says so.

### Reuse this pattern

- **Put every figure in a typed tool.** Declare each computation as a tool with a schema, and let the model only choose tools and explain their results.
- **Write the record on the server.** Write one content-free record per turn from the API, whatever the model does, so every answer and refusal is traceable.
- **Sample, alarm, never gate.** Score a sample of answers against the evidence they had, alarm on the aggregate, and keep soft scores out of release decisions.
- **Version prompts and gate activation.** Make each prompt version immutable and activate it only after its calibration cases pass.

## Design highlights

- **The answer streams as written and is never rewritten.** No text filter edits it after the model writes it; instead the tools own every figure, and audit and evaluation govern the result ([ADR-083](../../decisions/adr-083-stream-the-agent-answer-and-govern-it-by-audit-and-evaluation.md), [ADR-082](../../decisions/adr-082-govern-the-agent-by-evaluation-and-audit-not-text-heuristics.md)).
- **The end user's identity travels to every hop.** Token exchange carries the signed-in user from the route through the agent and AgentCore Gateway to each tool, which verifies it ([ADR-077](../../decisions/adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md)).
- **Memory is context, never evidence.** Long-term memory holds context such as a user's goal, and measured figures are kept out of it ([ADR-147](../../decisions/adr-147-treat-long-term-memory-as-context-never-evidence.md)). The Known limits row **Rely on chat never restating a remembered figure** records what has been checked live.

## Deploy it

- It deploys with the full solution through `make deploy-all`; follow the hub's [Quickstart](../../../README.md#quickstart). There is no separate assistant deploy.
- Enable Amazon Bedrock access to the three models the hub's [Prerequisites](../../../README.md#prerequisites) lists.
- Before data lands, chat can answer "What metrics can I ask about?" but has no figures to give. [Load your data](../../../README.md#load-your-data) says how data arrives.
- Online evaluation is optional; read [Optional features](../../../README.md#optional-features) first.

## Adapt it

| To change | Edit |
|---|---|
| An agent tool | [`src/adapters/inbound/tool_handlers/schemas.py`](../../../src/adapters/inbound/tool_handlers/schemas.py) and a handler beside it |
| The agent's instructions | [`contracts/prompts/v1/managed-conversation.md`](../../../contracts/prompts/v1/managed-conversation.md), then the [agent regression](../../../validation/README.md#agent-mode-regression) |
| Insight prompts | [`contracts/insights/`](../../../contracts/insights/), [`contracts/prompts/insights/`](../../../contracts/prompts/insights/); activate new versions in Prompt administration |
| The models | `ADLYTICS_*_MODEL_ID` in `.env` |

The command to run after each edit is in the hub's [Extend it](../../../README.md#extend-it) table.

## Learn more

- [Workspace assistant](../../ui/workspace-assistant.md): the chat panel, streaming and page context
- [Conversational runtime](../../capabilities/conversational-runtime/README.md): the agent, tools and stream contract
- [Agent quality loop](../../capabilities/agent-quality-loop/README.md): audit records and evaluation
- [Protected quality review API](../../capabilities/protected-quality-review-api/README.md)
- [Prompt administration](../../ui/prompt-administration/README.md)
- [Observability](../../concepts/observability.md) and [semantic accuracy](../../concepts/semantic-accuracy.md)
- [Answerable questions](../../answerable-questions.md)
- [Platform](../platform/README.md): the data, metric layer and secure edge the assistant reads through
