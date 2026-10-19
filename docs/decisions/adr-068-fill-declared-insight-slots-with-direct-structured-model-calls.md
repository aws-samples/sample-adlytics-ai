# ADR-068: Fill declared insight slots with direct structured model calls

Date: 2026-09-15
Status: Accepted — implemented in development; production acceptance not claimed

> **Amendment, 2026-10-05 — an evaluated-quiet slot serves why it went quiet (W13).** This
> reverses one clause of the 2026-09-23 amendment below: the served reason for an
> evaluated-quiet slot is no longer the contract's `silence_condition`. Gate-1 goes quiet for
> four reasons, decided in order: a required input is missing, a required finding kind is
> missing, every finding of a required kind was removed only by the near-zero prior bound,
> or no finding of a required kind clears its threshold. One governance sentence would be
> false for most of them, and "change" is wrong on the share slots. `predicate_admission`
> returns the reason with the findings, and the serve path serves one customer string per
> (slot, reason) from `INSIGHT_QUIET_COPY`, failing closed on an undeclared pair. A slot the
> model declined serves its own string ("Nothing here stands out beyond what the chart
> already shows."). The contracts keep `silence_condition` as their governance statement.
> The ineligible class and its missing-evidence sentence are unchanged.

> **Amendment, 2026-09-24 — tiered narration; grounded citations; tool-use envelope
> ([ADR-076](adr-076-tier-insight-narration-across-card-and-deep-dive-models.md)).**
> Four decision points and one earlier amendment change under ADR-076, which owns the
> details. Decision point 2 (arithmetic is server work) is revised per ADR-076 decision 2
> *as amended the same day* (its verified-derivation first cut was rejected as brittle
> before commit): the deterministic gate is the grounded `cited_figures` list — enforced
> for every output class — plus wire-token rejection; the model's prose reasons with the
> chat agent's liberty, and its figure-faithfulness and label honesty are
> calibration-owned. The tools-compute invariant is reconciled through the citation list
> (every cited figure is tool-sourced, displayed as provenance), with the prose residual
> risk accepted and stated in ADR-076.
> Decision points 3 and 7 (native structured output) are superseded by forced tool use;
> the target models refuse the native parameter. Decision point 5's cache identity gains a
> tier dimension, with the deep tier keyed by the deep prompt's own version. The
> 2026-09-15 declared-input-class amendment (context depth and voice, item 3) gains one
> class: a sibling preset's baked
> artifact from the same publish cohort, direction only, admitted solely for the deep
> tier's cross-horizon perspective.

> **Amendment, 2026-09-23 — every non-filled slot names its absence class and customer
> reason.** The served slot payload gains two nullable fields: `absence_class`, a closed
> vocabulary (`evaluated-quiet`, `ineligible`, `generation-pending`, `out-of-slice`), and
> `absence_reason`, a governed customer-legible sentence — so no slot renders an unexplained
> blank (the declared-outcomes rule). The
> wire states stay exactly `filled | silent | unavailable`. Classification happens where the
> knowledge lives: request derivation classifies each declared slot as admitted,
> evaluated-quiet (complete evidence whose predicate admitted nothing; the reason is the
> contract's own `silence_condition`), or ineligible (a required evidence input absent for
> this subject and window; the reason names the missing inputs in plain words). The serve
> path stamps `generation-pending` on a request that went unanswered, and compositions that
> never generate declare `out-of-slice`. Evidence availability is declared from what the
> artifact actually delivered, never the static contract shape — a claimed-but-absent input
> would misclassify ineligible as evaluated-quiet. A model that evaluates the findings and
> declines shares `evaluated-quiet` with Gate-1 refusal; `prompt_version`
> (`deterministic-gate1` versus the active immutable version) preserves provenance. The
> cache row keeps its seven-field ops shape and `unavailable_reason` stays an ops-only
> diagnostic; the serve path overlays the taxonomy on every slot it composes. The slot
> payload follows one shape rule: the absence pair is present exactly when an absence is
> declared, and decoders accept that closed shape family (pair present together or absent
> together, class never null). A slot with no declared absence — every filled slot, and
> slots persisted before the taxonomy — carries the seven base fields only, which is what
> lets artifact codecs keep their exact-readback re-attestation over immutable retained
> bodies without any migration. (An always-emit encoder shipped briefly on 2026-09-23 and
> broke exact readback of retained campaign bodies with `ADLY-PAGE_CONTEXT-0004`; the
> shape rule above is the corrected decision. Bodies baked while it was live would carry a
> null pair the corrected decoder refuses; enumeration of every dev pointer body and the
> bucket's newest objects verified no bake ran inside that window, so no such body exists
> and none needed retiring.) UI rendering of the classes follows separately under the
> mockup/design-revision discipline.

> **Amendment, 2026-09-22 — join the bounded on-demand roster; cache retention cannot
> invalidate exact content.** Live investigation found all current 1-day, 7-day, and 14-day
> segment-zero portfolio contribution/exposure rows already stored as filled or silent under their
> active prompt versions, while an exact adapter read raised `ADLY-INSIGHT-0003`. Each row carried
> `superseded_at`: segment one has a different artifact revision under the same analytical scope,
> subject, window, and slot, and its successful cache write had marked every different revision —
> including the simultaneously serveable segment zero — for retention. The reader then treated that
> cleanup marker as malformed exact content. This was the mechanism behind artifact-available,
> insight-unavailable responses; Bedrock and the artifact were not missing.
>
> The request path already launched direct model calls in a configured bounded thread pool and
> rejoined by slot position, but serial prompt/cache pre-reads plus a four-second application cutoff
> could convert healthy work into absence. The owner selected one request-time rule: after exact-cache
> lookup, launch every admitted miss concurrently and join all to a generated result, verified silence,
> or classified failure; do not apply a shorter application deadline. External provider transport and
> request-platform ceilings remain. Cache validity is the complete content identity — analytical scope,
> subject, resolved window, artifact revision, prompt version, and slot — never retention time.
> `superseded_at` is cleanup metadata and successful generation cannot infer sibling segment
> obsolescence. Optional prewarming remains correctness-neutral; ADR-075 owns its later migration.
>
> A direct diagnostic also exposed duplicate citation strings, which the cacheable narration invariant
> rejects. A first implementation attempted to push uniqueness/nonblank constraints into Bedrock's
> native schema; live acceptance returned provider `ValidationException` because Bedrock's supported
> JSON Schema subset excludes `uniqueItems` and string constraints. Native schema therefore owns exact
> fields and basic types, while the adapter deterministically strips blank citations and de-duplicates
> the remainder before domain validation. Provider schema-validation failures are invariant defects,
> never dependency retries. The current latency, caching, recovery, telemetry, and cost mechanics are
> owned solely by the page [operational design](../ui/campaign-portfolio/operational-design.md).

> **Amendment, 2026-09-22 — findings precision is display-governed, and incomplete generations
> are typed.** Deterministic findings quantize division-derived values (percentage change, share
> of total) to two decimal places at the derivation seam (`calculate_deterministic_findings`);
> exact differences keep their operands' served display scale. Exact-decimal division had emitted
> 28-significant-digit rates whose verbatim citation contract (point 2 below) multiplied payload
> and model-output size until the structured output truncated inside its token budget, decoding
> as a generic malformed-output invariant and dead-lettering silently. Two companion controls
> ship with the quantization: a generation whose `stopReason` is not `end_turn` raises the typed
> `ADLY-INSIGHT-0012` truncation error carrying stop reason and token usage, and the continuation
> worker's final SQS receive degrades a still-failing generation to a durable `unavailable` cache
> row with a bounded reason code — a served, diagnosable absence instead of a silent dead letter
> (a missing immutable prompt version degrades the same way, on its first receive). A degrade row
> is a placeholder, never a tombstone: readers treat it as a regeneration miss while serving the
> absence, and the cache's one permitted replacement is unavailable → generated, conditioned on
> the exact degraded state it replaces — so a transiently failing slot heals on any later fill
> instead of freezing for the life of its revision. The on-demand amendment above supersedes
> deadline-triggered continuation; durable recovery remains only for classified dependency failure.

> **Amendment, 2026-09-19 — deterministic artifact publication and insight precompute are separate.**
> The deployed preset baker persists deterministic artifacts and does not invoke Bedrock or consume the
> insight cache. A separate publications-stream worker remeasures Campaign evidence to warm eligible
> revision-keyed slots, and Campaign serving reads or first-fills those slots. The cache, verification,
> silence, and optional-slot decisions below remain; [ADR-074](adr-074-define-authority-backed-data-flow-completion-and-service-level-objectives.md)
> makes AI enhancement a nonblocking child objective by default. The 2026-09-22 amendment above makes
> first-fill complete on demand and prewarming correctness-neutral.

> **Superseded implementation assignment, 2026-09-15
> ([ADR-070](adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md)).** This amendment
> originally assigned page-artifact persistence to Insight Precompute and described Campaign preset
> insight generation as always precomputed. The 2026-09-19 amendment above replaces that assignment:
> the publisher persists deterministic artifacts, Insight Precompute is a separate best-effort warmer,
> and serving first-fills exact misses. ADR-070 still withdraws Campaign custom windows.

> **Amendment, 2026-09-19 — preset slot findings from the shared generation materialization.**
> [ADR-075](adr-075-generate-page-artifacts-once-per-dependency-cohort.md) retires the separate
> remeasuring Insight Precompute worker: preset-artifact slots read deterministic findings from the
> page generation's persisted materialization and execute no Athena. Request-time tiers — the live
> detail tail and metric-focus first generation — are outside that amendment. Cache keying by artifact
> revision and prompt version, verification, silence, and optional-slot semantics are unchanged. The
> [generation materialization](../ui/generation-materialization.md) design owns the consumer contract.

> **Amendment, 2026-09-15 (context depth and voice).** Three refinements within the original
> invariants. (1) Every slot prompt follows the authoring standard owned by the
> [Prompt Administration low-level design](../ui/prompt-administration/low-level-design.md)
> and speaks in the platform's one voice — the conversational analyst persona owned by
> `contracts/prompts/v1/managed-conversation.md`. Prompt-level grounding rules are
> defense-in-depth; the architectural guardrails (closed inputs, figure verification,
> structured output, silence) remain the load-bearing controls. (2) A slot's declared inputs
> should be substantial: full relevant artifact sections, entity display context, and
> server-computed decompositions — not a pair of numbers — bounded by the slot's one declared
> purpose and its silence condition, which remain the defense against caption degeneration.
> (3) The **immediately-prior baked artifact** of the same (page, variant) is an allowed
> declared input class, enabling continuity findings ("continues last window's decline") from
> already-attested evidence with one read. Deeper lookbacks remain out of scope.


> **Amended 2026-09-27 by [ADR-082](adr-082-govern-the-agent-by-evaluation-and-audit-not-text-heuristics.md).** The deterministic insight gate (the cited-figures
> subset check and the wire-token check) is removed. A narration may state figures derived from its
> supplied findings; figure faithfulness is owned by calibration and evaluation. The "Let the model
> compute statistics from raw series" rejection below still holds for raw series: the model reasons
> over the deterministic findings it is given, not over data it was not.

## Context

The command-center pages ([Campaign Portfolio](../ui/campaign-portfolio/README.md)) carry
AI-authored insight cards beside deterministic evidence. The conversational runtime cannot serve
them: a managed Harness turn plans, iterates tools, and finalizes in tens of seconds, while a
page insight must render with the page and re-resolve on every window change. The
[visual narration harness](../ui/visual-narration-harness.md) already decided that page
narration is server-owned, built from the resolved artifact, and never routed through the chat
conversation; this decision supplies its generation mechanics and extends it to purpose-typed
slots.

Two risks shape the design. Unbounded "insights" degenerate into chart captions — restating the
delta a chip already shows. And any model path that computes its own numbers reopens the
fabrication door the platform exists to close.

## Decision

**A page declares a fixed roster of purpose-typed insight slots; the server computes each
slot's deterministic findings, one direct structured-output model call per slot narrates them
or stays silent, and results are cached by content revision — never by time.**

1. An insight slot is a declared contract: purpose (the one inference it exists to make), the
   artifact inputs it reads, the deterministic pre-computations the server supplies,
   a silence condition, an output class (Observed, Perspective, or Hypothesis, per the
   narration contract), and a versioned task prompt (ADR-069). Slots are product decisions;
   adding one is a reviewed contract change with calibration cases, not a prompt tweak.
2. Arithmetic is server work. Contribution decompositions, funnel stage ratios, concentration
   and exposure sums are computed by domain code and handed to the model as findings. The model
   selects salience and phrases implication; every figure it may cite exists verbatim in its
   input, so the server verifies each Observed sentence's numbers against that input before
   caching, and rejects the generation on a miss.
3. Generation is one Bedrock Converse call per slot with a structured output schema — no agent
   loop, no tool selection, no conversation session. Every admitted cache miss for one artifact
   launches concurrently under a configured bound and rejoins by declared slot identity. A
   Hypothesis must propose only a read this platform can serve.
4. Silence is a valid, verified output. A slot below its declared materiality renders nothing;
   slots are never padded. The request waits for all admitted parallel calls rather than applying
   a shorter application deadline. A generation or dependency failure is contained to that slot
   and never fails independently completed siblings or deterministic page evidence.
5. The complete cache identity is (analytical scope, subject, resolved window, artifact revision,
   prompt version, slot). A data republication changes the revision and thereby invalidates; a prompt
   activation changes the version and invalidates exactly one slot. Time never determines validity.
   Retention metadata does not invalidate an exact-key hit, and a cache write cannot infer that sibling
   segment revisions are obsolete. A miss first-fills directly at serving; optional prewarming writes
   the identical key but never gates or changes request correctness. Campaign custom windows remain
   withdrawn under ADR-070.
6. Every cached insight stamps the artifact revision and prompt version that produced it
   (configuration provenance per ADR-062).
7. Every provider call emits bounded structured latency and outcome evidence. Native structured
   output enforces exact fields and basic types; the adapter canonicalizes citations to unique nonblank
   values before domain verification remains the final gate.

## Consequences

The first viewer of an admitted exact revision waits for the slowest parallel direct call, not the sum
of slot latencies; subsequent viewers read the revision-keyed cache. There is no four-second
application cutoff that converts still-running work into a blank card. External provider transport
and request-platform ceilings still fail safely and are measured rather than implied absent.

Optional prewarming can reduce first-view latency, but it is not authority and cannot invalidate a
simultaneously serveable sibling artifact. Durable recovery is reserved for classified dependency
failure; malformed or invariant-violating model output remains a visible diagnostic requiring a
schema/prompt fix rather than repeated identical work.

The verification step is cheap precisely because generation is structured and closed-input: checking
cited figures against the input is string and number matching, not semantic review. The slot roster
remains a governed vocabulary; a new inference kind ships with declared inputs, a silence condition,
and calibration cases.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Route page insights through the managed Harness | Tens of seconds per surface and a tool loop with nothing to decide: the artifact is already resolved. Wrong latency class, wasted machinery. |
| Let the model compute statistics from raw series | The fabrication door. A model-derived ratio is a figure no tool computed; the platform's central invariant is that tools compute and the model narrates. |
| Free-form narration without declared slots | Degenerates into chart captions; no silence condition means padding; no purpose means no calibration target. |
| TTL-based validity | The source is versioned; time either serves stale insights after a republication or regenerates unchanged ones. Content revision is the correct and free invalidation. Retention TTL is cleanup only. |
| Return after a short application deadline and finish normal calls in the background | Converts healthy 1–4 second direct calls into visibly blank cards, duplicates work, and made response state depend on scheduling. Rejected in favor of joining the bounded parallel roster. |
| Serial on-demand calls | Makes page latency the sum of independent model calls. Rejected; bounded parallelism and deterministic positional join are required. |
| Remove optional prewarming immediately | Not required for request correctness. ADR-075 already owns retirement of the remeasuring worker and the target zero-measurement warmer; this amendment removes prewarming from the correctness path without expanding this incident slice into that migration. |

## Review trigger

- A slot needs multi-subject evidence beyond its page artifact, which argues for widening the
  declared inputs rather than giving the generator tool access.
- Verification rejects a material fraction of generations, which means prompts or schemas need
  work and the calibration corpus is missing a case family.
- Deployed first-generation p95 approaches the request-platform ceiling, which requires progressive
  delivery or precomputation rather than another timeout change.
- The slot roster grows past what a page can render, which argues for server-side salience
  ranking across slots — a new decision.
