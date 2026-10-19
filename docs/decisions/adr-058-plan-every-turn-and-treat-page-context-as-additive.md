# ADR-058 — Plan every turn and treat page context as additive

| | |
|---|---|
| **Status** | Superseded — ADR-006 owns the current managed-Harness contract; the former page-context decision is retained only as historical rationale. |
| **Date** | 2026-09-02 |
| **Decision** | Historical: this ADR recorded a now-retired additive page-context approach alongside the former planning mechanism. It defines no current route input, tool authority, or response behavior. |
| **Related** | [Conversational Runtime](../capabilities/conversational-runtime/README.md) · [Analytics Discovery](../capabilities/analytics-discovery/README.md) |

## Context

A lexical social-conversation escape path can bypass planning, evidence seeding, prompt receipts,
admission, publication, and retention. It fails on natural messages that mix onboarding, capability
discovery, metric education, page interpretation, and analytical questions. A whole-turn classifier
would make wording a release authority and cannot preserve mixed objectives.

The product is a strategic analytical counterpart, not a report reader. Users must be able to ask what
a metric means, why it matters, how metrics relate, what a movement might imply, and for an advisor
perspective. At the same time, the agent must not present its reasoning as a current fact about the
customer, the active deployment, or an existing page.

The managed Harness separates conversational iteration and final prose from repository-owned semantic
tools and the API release boundary. The active metric catalog and page declarations remain server-owned
authorities for product claims.

## Historical decision

**This ADR records a superseded approach.** ADR-006 now owns the managed-Harness route contract,
which sends the verified session and question without the retired page-context mechanism.

All governed tools remain globally available under the deployment-wide `AuthorizedAnalyticalScope`. The
model supplies semantic candidates; deterministic tool code validates authority, scope, evidence,
action, and budget and returns qualified results or typed limitations. Harness preserves explicit
intent, naturally discusses and combines those results, and produces the first-class answer. Harness
may make no tool call for a social message or to explain that an unrecognized or unsupported metric
request cannot be mapped, provided it makes no customer-specific claim or figure, runs no substitute
query or tool, does not silently change or drop intent, and asks for clarification or offers next
steps. Social wording in a mixed-objective turn never permits an analytical objective to be skipped; a
typed tool refusal remains equally valid.

The server applies authority per visible content:

| Content | Authority | Customer-data evidence required |
|---|---|---:|
| Safe social or unmappable unsupported-metric limitation | Harness final prose after managed lifecycle validation and outbound Guardrail assessment | No; must make no customer-specific claim or figure, execute no substitute query or tool, preserve intent, and ask for clarification or offer next steps |
| Conversation | Harness final prose after managed lifecycle validation and outbound Guardrail assessment | No; cannot establish customer/current-platform facts |
| Metric education | Model knowledge, augmented by catalog metadata when explaining this platform's metric variant | No, unless it makes a current customer claim |
| Advisor Perspective | Model judgement about importance, priority, tradeoffs, or implications | No for general advice; yes when it asserts a customer-specific premise |
| Hypothesis | Model reasoning expressed as a possibility with a validation direction | Current evidence when tied to a customer-specific premise |
| Capability/orientation | Server-built declaration from catalog, declared pages, and declared actions | No; never claims current customer availability without coverage |
| Analytical observation/diagnosis | Governed operation plus current-turn evidence | Yes |

Education and perspective are not loopholes for factual authority. General knowledge may explain
common metric relationships or possible implications. It may not claim what happened to this customer
without evidence, what this deployment currently contains without server authority, or that a proposed
allocation will cause a business outcome.

Capability/orientation may describe `workspace` or a server-resolved declared destination, but it never
creates authorization. A genuine unsupported semantic operation may produce either a safe no-tool
published limitation or a typed tool refusal; neither may become substitute success. Dependency
failures and policy decisions retain their typed refusals. The model never authors a page ID, browser
route, catalog selector, visual schema, or data query.

Pages remain purpose-built, deterministic visual surfaces. They own charts, tables, controls, and
typed artifacts; no agent creates page layout or visualization.

## Consequences

A turn can combine orientation, education, advisor perspective, clarification, and analytical work. For example, a user can ask what the platform does, what ROAS means, and how to interpret the active page without losing a later evidence-bound tool call.

Harness manages conversational iteration and returns the first-class prose answer; deterministic tool
code still validates every model-authored semantic candidate submitted to a tool and produces the
qualified facts the answer may use. The API validates the managed lifecycle, applies the outbound
Guardrail to the exact final prose, and performs only wording-preserving structured normalization into
`AssistantResponse.v2`; presentation-only Markdown delimiters may be removed, but wording may not
change. It then audits and evaluates the answer. A safe no-tool limitation records
`operation_outcome=not_attempted`, `publication=published`, and `terminal=complete`; a typed tool
refusal remains equally valid and records `operation_outcome=refused`. Dependency failures, invalid
lifecycle, and policy interventions retain their existing closed terminal behavior.

The natural answer must make Evidence, Advisor Perspective, Hypothesis, Next Existing Action, and Boundary intent explicit when those roles are present. A customer-specific recommendation may combine observed evidence and advisor perspective, but its wording must not present judgement as a measured result; the API does not rewrite it into semantic blocks or rendering identifiers.

The former context-assembly design is historical. ADR-006 owns the current managed-path input contract.

The historical proposal required scenario coverage for novice language, role-appropriate education,
strategic perspective, complex multi-objective questions, evidence/perspective separation, page
relevance, safe no-tool limitation quality, and typed refusal behavior. Current quality ownership is in
the [Agent Quality Loop](../capabilities/agent-quality-loop/README.md); it does not restore the retired
endpoint harness.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Expand greeting/thanks expressions | Phrase maintenance cannot represent mixed objectives and creates a second release path. |
| Add a separate intent classifier or routing path | Duplicates the multi-objective planner, creates competing interpretations, and gives wording a release path. The accepted verifier is a mandatory plan-bound veto inside the same transaction; it cannot route, rewrite, or release. |
| Treat browser page state as whole-turn scope | Lets a browser view override explicit workspace intent. |
| Let model knowledge establish product capability | Produces unverifiable deployment claims; capability remains server-owned. |
| Suppress all non-evidentiary reasoning | Removes the strategic-advisor value of GenAI and confuses education with customer diagnosis. |
| Let the agent generate page layout or charts | Breaks page-purpose ownership and lets model output become a visual/data authority. |

## Review trigger

Revisit this decision when:

- the managed Harness cannot meet agreed scenario quality, latency, or cost targets;
- a capability needs an authority source outside catalog/page declarations;
- perspective labels or evidence separation prove insufficient in user testing;
- arbitrary page/visual generation is proposed; or
- managed semantic-candidate or direct-result validation cannot meet agreed refusal and adversarial-scenario quality; or
- no-tool handling is proposed beyond social or unmappable unsupported-metric limitations or would permit customer-specific claims, figures, substitute operations, or weakened intent.
