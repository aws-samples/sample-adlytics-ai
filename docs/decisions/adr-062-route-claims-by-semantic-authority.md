# ADR-062: Route claims by semantic authority

Date: 2026-09-10
Status: Accepted 2026-09-10 — refusal evidence, shared metric resolution, prompt-level authority routing, and per-step audit selection and reason codes implemented; the typed per-block authority contract, per-claim publication, the remaining configuration provenance, and purpose-bound capture pending; a refusal superseded by a later success of the same tool stops being an obligation per ADR-066

> **Amendment, 2026-09-25 — status reconciled with the shipped implementation.** The previous
> status listed authority routing as pending although the 2026-09-10 amendment below records the
> prompt-level routing policy as implemented; the 2026-09-13 dev acceptance answered a refusal-probe
> question as general knowledge without a tool call under that policy
> ([validation results](../../validation/RESULTS.md)). The audit receipt's per-tool-step
> `selected_into_plan` flag and typed violation codes are implemented (`AuditToolStepV3` in
> `src/domain/conversation/audit.py`). Its configuration provenance carries the prompt catalog
> digest, Guardrail version, and contract versions but not yet the tool-schema digest,
> metric-dictionary version, or model identifier (`ConversationAuditRecordV3`). No code-owned
> authority-class enum, per-claim publication, required/supplemental dependency classification, or
> purpose-bound capture exists under `src/` as of 2026-09-25.

> **Implemented 2026-09-10 — refusals are evidence.** Deferred metrics resolve by normalized
> canonical name, customer label, and governed alias through the same surfaces as computable
> metrics; refused tool results retain their reason and grounded refusal projection in custody;
> and limitation notices may reference refusal renderings during verification and
> materialization.

> **Implemented 2026-09-10 — authority-scoped routing policy.** The agent prompt and tool
> descriptions route by claim authority: governed tools are required for platform and customer
> claims only, general explanation is answered without tools, refusals are presented through
> their renderings, and the grounded-correctness evaluator scopes its judgment to platform and
> customer claims. The typed per-block authority contract, per-claim publication, and
> purpose-bound capture remain pending.


> **Amended 2026-09-27 by [ADR-082](adr-082-govern-the-agent-by-evaluation-and-audit-not-text-heuristics.md).** The pre-publication authority check between block
> classes is withdrawn, and the typed per-block authority contract is no longer a publication
> control: authority routing and leakage between classes are measured by evaluation. The
> numeric-literal restriction this ADR planned to make authority-aware is removed instead. An
> inference may carry figures validly derived from the turn's governed results.

## Context

A deployed turn asking what a deferred metric means terminated in a generic unavailable message. The catalog resolved deferred metrics only by internal key, so a customer-facing label reported `unknown_metric` instead of `not_yet_computable`; tool custody stripped grounding from the refused result; and post-Harness verification then rejected an otherwise valid plan that referenced the refusal. The turn also exposed a policy defect independent of those bugs: the active prompt routes any claim about "metrics" to governed tools and restricts free prose to non-factual text, so a general educational question the model can answer directly is forced through the catalog and fails with it.

[ADR-056](adr-056-publish-conversation-as-evidence-bound-semantic-blocks.md) and [ADR-061](adr-061-bind-agent-authored-presentations-to-governed-evidence.md) bound data-bearing values to governed evidence but treated every factual assertion as governed. [ADR-059](adr-059-create-a-bounded-agent-quality-audit-loop.md) and [ADR-060](adr-060-protect-quality-review-retrieval-with-an-explicit-operator-authorization-boundary.md) made terminal audit receipts content-minimized, which protects customer content but leaves the improvement loop unable to see why routing or resolution failed.

## Decision

**Every claim in a response is routed, published, and evaluated by its semantic authority — the kind of evidence it needs — never by its subject. The audit receipt stays content-free, and the feedback loop is served by typed reason codes, configuration provenance, and purpose-bound content capture inside the evaluation custody boundary.**

The closed authority classes are `general_knowledge`, `platform_catalog`, `customer_measurement`, `user_premise`, `conditional_inference`, `recommendation`, and `unknown`. A governed tool is required only for `platform_catalog` and `customer_measurement` claims. A general explanation or a hypothetical needs no tool even when it names a declared metric. A term that may be proprietary and cannot be classified resolves to `unknown` and produces a clarification, never an invented platform fact.

A typed refusal is evidence. It retains its grounding, carries a publishable rendering, and blocks only the claims that depended on it. Each response dependency is `required` or `supplemental` relative to the requested claims: a failed supplemental dependency degrades the response by omission, while a failed required dependency produces an explicit limitation. Publication accepts or rejects each governed claim with its qualifiers as one unit rather than accepting or rejecting the whole turn.

Every customer-facing metric reference — computable or deferred — resolves through one shared resolver over the surfaces the metric dictionary declares. No tool exposes a stricter matching surface than another.

The audit receipt additionally records, per tool step, a typed reason code and whether the result was selected into the published plan, and per turn, the configuration provenance it ran under: prompt contract version, tool-schema digest, metric-dictionary version, and model identifier. Customer content needed by the improvement loop — an unresolved reference text, the answer strategy — is captured only with a declared purpose, sensitivity tier, retention, and access boundary, inside the evaluation custody boundary that already holds the exact question and released answer. The receipt itself remains content-free, so ADR-060's authorization boundary is unchanged.

Evaluation is authority-scoped. Governed claims are scored against governed evidence only. General explanations are scored for correctness and appropriate scoping, not for catalog support. Routing itself is evaluated: unnecessary tool use, missed required tools, and authority leakage between blocks are observed dimensions.

## Consequences

The prompt, tool descriptions, presentation plan, verifier, and evaluators change together: authority becomes an explicit block property, free prose is no longer defined as non-factual, and the numeric-literal restriction becomes authority-aware so a user-supplied hypothetical number is legal in an inference block while an observed customer figure still requires an evidence reference.

Routing classification is model judgment guided by closed classes, so it cannot be made deterministic. Deterministic verification still owns evidence integrity; a pre-publication authority check owns leakage between classes; and validation exercises question families over holdout concepts the implementation has never seen, not term lists.

Partial publication replaces all-or-nothing turns. A turn can now release a valid general explanation while a supplemental governed enrichment is omitted, which means a terminal outcome is no longer synonymous with every dependency succeeding.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Retract content minimization and store content in the audit receipt | Reopens the retention and access questions ADR-060 answered; the loop needs specific fields, not blanket capture. |
| Route by subject with term allowlists | Static, grows stale, and fails on the first unseen concept — the defect this decision removes. |
| Prompt-only routing policy | Influences but cannot be observed, tested, or enforced; the incremental-ROAS turn already had prompt-encouraged routing. |
| A separate classifier service as sole routing authority | A single opaque judgment replaces another; adopted instead as an observable strategy signal checked at publication. |
| Keep all-or-nothing publication | Discards valid work whenever any dependency fails and converts partial capability into total unavailability. |

## Review trigger

Revisit when the authority classes are implemented as a code-owned enum and contract (ownership of the class list then moves there), when AgentCore supplies trusted shared turn state enabling in-loop repair, or when routing evaluation shows the authority check failing to catch leakage at the accepted quality target.
