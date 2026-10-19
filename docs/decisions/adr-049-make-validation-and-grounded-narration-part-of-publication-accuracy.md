# ADR-049: Make validation and grounded narration part of publication accuracy

Date: 2026-08-24
Status: Accepted for canonical data publication and numerical grounding; exact-render-only conversational publication is superseded by [ADR-056](adr-056-publish-conversation-as-evidence-bound-semantic-blocks.md)

> **Decision supersession, 2026-09-10.** The requirement that every customer-specific numerical claim
> close over deterministic structured evidence and every mandatory qualifier remains in force.
> [ADR-061](adr-061-bind-agent-authored-presentations-to-governed-evidence.md) now owns the executable
> claim, collection, qualifier, and semantic-verification binding used by flexible agent-authored
> presentations. The exact-render-only mechanism below remains historical.

> **Implementation amendment, 2026-08-29.** Answer publication now distinguishes social
> conversation from analytical publication. A complete, narrowly classified social utterance may
> release natural model prose after the outbound content Guardrail, but receives no authority to
> state customer-data, metric-availability, capability, or performance facts. Every analytical or
> capability turn still requires a tool result, nonce-bound `GroundingSet`, exact rendering
> selection, and all mandatory qualifiers. Deterministic renderings are customer-readable sentences
> rather than key/value diagnostics. This corrects the earlier implementation's application of the
> analytical evidence gate to greetings without weakening numerical publication accuracy.

> **Implementation amendment, 2026-08-24.** Both publication boundaries are implemented and checked
> locally. V2 source and query packs feed candidate-scoped DQ; canonical publication selects the
> exact validated publication under ADR-047. Answer publication builds a nonce-bound `GroundingSet`,
> reads it strongly, requires complete typed qualifiers, accepts only exact rendering selections,
> discards model prose, and releases deterministic text. Named-stack inspection, synth/diff,
> deployment, live publication, cutover/V1 retirement, eight governed AMC pack executions, and
> browser acceptance remain pending in
> [validation results](../../validation/RESULTS.md#pending-acceptance--2026-08-24). Mutation
> diagnostics still have survivors and are not represented as clean local acceptance evidence.

This established the Semantic Architecture V2 publication-accuracy boundary before prepublication DQ,
source packs and query packs were implemented. The original rationale and rejected alternatives below
are retained as the decision history.


> **Amended 2026-09-27 by [ADR-082](adr-082-govern-the-agent-by-evaluation-and-audit-not-text-heuristics.md).** A customer-specific figure the agent states may be
> returned by a governed tool or validly inferred from returned figures; it is judged against the
> turn's evidence by evaluation, not refused by a text check. Mandatory qualifiers remain in force
> and remain enforced structurally. Canonical data publication and its validation are unchanged.

## Context

A pipeline can publish bytes successfully and still publish a wrong analytical claim. Schema-valid
rows may carry an unresolved marketplace, a period total in a daily fact, incompatible metric axes,
a failed control total, or coverage widened across a gap. A query can then compute exactly what was
stored and a model can narrate a number that was never present in the structured result. Transport
success at either boundary is not publication accuracy.

There are two publication boundaries with one invariant:

- **data publication** makes a canonical partition current; and
- **answer publication** releases numerical claims to a user.

The first needs semantic and data-quality evidence before current data changes. The second needs every
numerical claim grounded in deterministic structured output before text is released. The detailed
load sequence belongs to [load-path](../capabilities/load-path/README.md), metric planning to
[query-path](../capabilities/query-path/README.md), and the response and release boundary to
[Conversational Runtime](../capabilities/conversational-runtime/README.md).

## Decision

**Publication accuracy is the conjunction of prepublication semantic validation, prepublication data
quality, and grounded numerical narration. Passing only one or two is insufficient.**

### Canonical data publication

Before a canonical publication can become current, its owner must produce validation evidence that:

- delivery and source provenance are complete and resolvable;
- commerce, marketplace, and product identities satisfy
  [ADR-045](adr-045-separate-delivery-commerce-marketplace-and-product-identities.md);
- row grain, measurement time class, units, basis, options, and canonical axes match the target
  declaration;
- additive, periodic, snapshot, and non-additive measures obey their publication rules;
- contract assertions, table-level DQ rules, control totals, and declared reconciliation checks pass;
- exact row/bucket coverage is derivable without widening; and
- the publishing owner and lease/fencing authority are valid.

Validation runs against the candidate publication, not against already-current rows. Failure records
evidence on the attempt, creates no successful publication, extends no coverage, and leaves the last
good publication current. A warning is publishable only when its typed rule explicitly permits a
qualified publication and defines the caveat; an untyped warning is a failure.

The successful publication references the validation rule-set version and its results. Re-running the
same candidate under changed rules therefore produces new evidence rather than rewriting history.

### Answer publication

Every user-visible numerical claim must be grounded in a typed value or caveat emitted by deterministic
query and domain code. The grounding records the structured value path, units, grain, coverage, basis,
and deterministic transformation used to render the claim.

The accepted decision allows natural conversation outside analytical publication while keeping every
customer-data claim evidence-bound. A complete social utterance may release brief model prose only
when no tool or grounding metadata was produced, and only after the independent outbound content
Guardrail. Greetings embedded in analytical requests remain evidence-required; questions about data,
metrics, capability, trends, comparisons, or performance cannot use the social path.

For analytical publication, the model emits exact rendering selections, model-authored analytical
prose is discarded, and deterministic code validates membership and renders customer-readable
sentences from the nonce-bound `GroundingSet`. It cannot introduce arithmetic, convert units, infer
totals, interpolate missing values, or select a numerical claim absent from the grounded set. An
invalid or contradictory selection withholds the narration and returns a useful non-evidentiary
reason; it is never masked and released as an answer.

Coverage gaps, suppression, mixed basis, incompatible axes, periodicity, truncation, and qualified DQ
outcomes are part of grounding when applicable. Approved resolution transparency is fixed factual
context, not model prose. A number without its mandatory qualifier or factual context is not an
accurate publication of that number.

## Consequences

**Accuracy becomes end-to-end and executable.** A successful write, query, or model invocation is
necessary operational evidence but no longer sufficient acceptance evidence.

**The last good data remains readable on validation failure.** Attempt history records the candidate
and its evidence, while [ADR-047](adr-047-separate-attempts-leases-publications-and-coverage.md)
prevents failure from becoming publication or coverage.

**DQ moved before publication.** Generated candidate-scoped rules and typed outcomes are evaluated
before V2 `CURRENT` changes. At adoption, generated-but-unevaluated rules could not satisfy this
decision; contract assertions were useful evidence but were not relabelled as complete
prepublication DQ.

**Narration may fail after correct computation.** That is a release refusal, not data suppression and
not a query failure. The structured result and validation evidence remain available for diagnosis,
while no ungrounded answer reaches the user.

**Qualified values stay qualified across every layer.** A downstream formatter cannot discard a
mandatory caveat merely because the numeric value itself is unchanged.

**Latency and test surface increase.** Candidate validation and narration release checks add work.
They are accepted because a faster plausible wrong answer violates the platform's primary quality
goal.

## Alternatives rejected

| Rejected | Why |
|---|---|
| Validate only schema and types before publication | Well-typed rows can still carry wrong identities, time classes, axes, control totals, or coverage |
| Publish first and run DQ afterward | Exposes a candidate before its accuracy is established and requires retracting already-read figures |
| Treat DQ failures as warnings by default | Converts a missing policy decision into silent publication |
| Rely on prompting to keep narration grounded | A prompt is not an executable proof that each numerical claim came from structured output |
| Release corrected or masked model text automatically | Hides that the candidate contradicted its grounding and makes the released statement a new untraceable transformation |

## Review trigger

Revisit when the narration surface can emit non-text analytical artefacts such as charts, tables, or
voice. The grounding invariant remains, but each renderer needs an executable correspondence between
its visible numerical claims and the structured result.
