# ADR-076: Tier insight narration across card and deep-dive models

Date: 2026-09-24

Status: Accepted 2026-09-24 (owner) — Phase 1 implemented and live-accepted 2026-09-25
(both tiers on production prompts, coordinated cutover complete; dated acceptance
entries in validation/RESULTS.md). Phase 2 (assistant/evaluator model consolidation)
remains pending. The steering carve-out named in Amends was approved and applied
2026-09-24

> **Proposed amendment, 2026-10-07.** [ADR-122](adr-122-declare-next-step-targets-as-a-typed-union-checked-against-navigation.md) (Proposed) would make a card's next step a typed union, `NextStep.v2`, with object targets checked against the navigation file. Slice 5 (2026-10-07) replaced this decision's next-step wire grammar with `NextStep.v2` in code, not yet deployed. Its other rules are unchanged, and steps stay dormant as below: every contract lists no kind.

Amends: [ADR-068](adr-068-fill-declared-insight-slots-with-direct-structured-model-calls.md)
decision points 2 (arithmetic prohibition — revised per this ADR's decision 2 as amended
2026-09-24: the deterministic gate is the grounded citation list plus wire-token rejection for
every output class; the tools-compute invariant is reconciled through the citation list — every
*cited* figure is tool-sourced and the dialog displays it as provenance — while prose is
narration whose figure-faithfulness is calibration-owned, an accepted and stated residual
risk), 3 and 7 (native structured output — superseded by
forced tool use), and 5 (cache identity — extended with a tier dimension); and
[ADR-069](adr-069-version-insight-prompts-in-a-calibration-gated-registry.md) decision points 1
and 3 and its activation model (the write-once version record gains fragment digests; the
one-mutable-activation-pointer-per-slot model becomes one pointer per slot per tier; the
calibration gate changes what "passed" attests). Decision 5's cross-horizon carve-out also
amends [ADR-068](adr-068-fill-declared-insight-slots-with-direct-structured-model-calls.md)'s
2026-09-15 declared-input-class amendment (context depth and voice, item 3): a sibling
preset's baked artifact (direction only)
becomes a new declared input class beside the immediately-prior artifact of the same variant.
On acceptance, each amended ADR gains a dated amendment block pointing here; the
[insight-presentation contract](../ui/insight-presentation-contract.md) gains the dialog
revision named in Consequences; and the window-scope integrity rule — whose home is the
`frontend-ui-standards` steering ("never render values from two windows side by side"), with
its narration-scope echo in [Decision Workspace UI](../ui/README.md) review criterion 3 —
gains the direction-only carve-out declared in decision 5. The steering edit ships only as
proposed content under explicit owner approval, per the working agreement; the carve-out keeps
the rule's letter for values (no second-window figure ever renders) and amends its scope
binding for direction words.


> **Amended 2026-09-27 by [ADR-082](adr-082-govern-the-agent-by-evaluation-and-audit-not-text-heuristics.md).** Decision 2's slim deterministic gate is removed: the
> `cited_figures` subset-of-supplied-values check and the internal wire-token check no longer
> refuse a narration, and the active prompts' arithmetic prohibition is replaced by the figure rule
> (valid inference from supplied findings, never fabrication). Inputs keep presenting internal
> states in customer wording. Calibration gains derived-figure cases before any prompt changes.

## Context

The insight slots are exploratory by design: their purpose is to spark strategic review, not to
restate tile facts. A 2026-09-24 released-output audit (evidence retained locally under
`validation/evidence/`) showed the current single-tier design failing that purpose from two
directions at once: narrations restate displayed values, and when they do explore they cross into
fabrication — unsupported causes, invented business outcomes, model-side arithmetic that no one
verifies, and raw wire tokens. The active prompts prohibit arithmetic entirely, which did not
prevent the arithmetic; it only guaranteed the arithmetic was unverified.

The owner's direction (2026-09-24): arithmetic where appropriate is fine with guardrails rather
than prohibition; a couple of sentences per card is right; and a click on an insight card should
open a deeper, longer, more exploratory analysis produced by a stronger reasoning model. Model
direction: retire Claude Haiku 4.5; use Claude Sonnet 5 for the quick tier and Claude Opus 5 for
the deep tier.

Verified Amazon Bedrock facts [2026-09-24, dev account, us-east-1]:

- `us.anthropic.claude-sonnet-5` and `us.anthropic.claude-opus-5` inference profiles exist.
- Both **refuse** the Converse native structured-output parameter (`output_config.format:
  Extra inputs are not permitted`) that `BedrockInsightGenerator` and the calibration adapter
  use today.
- Both **refuse** `temperature` as deprecated.
- Both **support** forced tool use (`toolConfig` + `toolChoice`) and returned the exact
  `insight-narration-v1` field set through it.

Configuration coupling: `config.agent.quickChat.modelId` and `config.agent.deepDive.modelId`
(from `ADLYTICS_QUICK_CHAT_MODEL_ID` / `ADLYTICS_DEEP_DIVE_MODEL_ID`) feed the chat assistant
harness, the evaluation judge stacks, prompt-registry calibration, and insight generation. The
chat harness additionally pins `temperature: 0` in its Bedrock model config, so repointing the
shared values at Sonnet 5 / Opus 5 without envelope work would break the assistant.

## Decision

### 1. Two delivery tiers per insight slot

> **Amended 2026-10-05 (deep-input supply; design §4.2).** Until this date page-slot deep
> received only sibling directions and detail deep received no enrichment, so "the full slot
> context" below was true for focus deep only. `src/adapters/deep_insight_inputs.py` now
> supplies every declared `deep.allowed_inputs` line from the exact served artifact: the
> daily series of each focusable metric (at most 14 days, each with its aligned prior), every
> total with its change, the slot's own drivers (at most one 50-row segment), and the basis
> and lag qualifiers. `ALLOWED_INPUT_SUPPLIERS` maps each declared line to the slots it is
> supplied for, and a contract line without a supplier fails `make verify`. Portfolio page
> deep states its scope: row-built entries carry "(campaigns in view)", totals carry
> "(whole portfolio)", and a "campaigns in view" sentence names which N of M campaigns the
> served segment holds; when one segment holds every campaign, rows read as the whole
> portfolio. Detail deep is bound to one redeemed selection and window and has no sibling
> roster, so the four detail contracts no longer declare the sibling line. W10: the
> divergence sibling metric is the portfolio ROAS total's own id, so its sibling entry now
> exists. The C7 "servable views" entry is built and tested but not wired until its harness
> A/B passes.

- **Card tier (quick route → Claude Sonnet 5).** Baked/cached as today; at most two sentences;
  exploratory reading permitted and labeled by its output class; silence stays a correct answer
  when the evidence adds nothing beyond the tiles.
- **Deep-dive tier (deep route → Claude Opus 5).** Generated on request when the customer opens
  the insight dialog — riding the on-request dialog read the insight-presentation contract
  reserves for its serving slice, extended by this decision from deterministic recompute to
  include the deep generation. It receives the full slot context (complete series, all drivers,
  cross-metric totals, qualifications) — typed as citable findings wherever they may serve as
  citations (per the amended decision 2); wording-only context remains uncitable in both
  tiers — plus a larger
  token budget, and returns a structured exploratory analysis: what stands out, up to three
  candidate readings labeled as readings, and the evidence that would discriminate between them.
  Results are cached under the card's identity dimensions (analytical scope, subject, window,
  artifact revision, slot — ADR-068 §5) with a tier discriminator and the deep prompt's own
  version, so deep prompt activation invalidates exactly the deep tier, every cached analysis
  stays attributable to the prompt that produced it, a repeat open is a cache hit, and Opus
  spend is metered by genuine clicks.

> **Amended 2026-10-05 (insight input identity; design §4.11, R4-3, R4-4, R5-5).** The cache
> identity above covered the artifact, not the model input, so a changed finding or context
> entry kept serving text built from the old input. The earlier promise that the card tier keeps
> its content key and that no cached row moves is withdrawn:
> - The card key carries `input_digest`, the SHA-256 of the canonical model input document
>   (`insight_input_document`; the Bedrock payload adds only `schema` and `silence_allowed`), as
>   `#INPUT#<digest>` on both tiers. A card generation whose key digest differs from its own
>   input is refused.
> - Deep enrichment runs only on a miss, so the deep key carries a declared
>   `deep_input_revision` (`#TIER#deep#DEEPINPUT#<revision>`) from
>   `src/adapters/deep_input_revision.py`. `tests/unit/test_deep_input_revision.py` hashes the
>   in-repository import closure of the deep-input code plus the metric dictionary and fails
>   until a new revision is appended, so any deep-input change moves every deep key.
> - Continuation messages carry both fields; a message from before this change is refused and
>   acknowledged, and its slot stays pending until it is next opened.
> - `insight.model.call` records `input_sha256` (the exact message sent) and
>   `cache_identity_sha256` (ownership and content key), never their content.
> - Cost: every cached card and deep row moves once. Where no refresh publication moves the
>   artifact revisions anyway, that is one full regeneration.
- The dialog opens instantly with the card content; the deep section fills under the existing
  skeleton discipline. If generation exceeds the request envelope, the existing
  generation-pending taxonomy and continuation queue carry it; the dialog re-fetches.

> **Amended 2026-10-05 (card next steps; owner 2026-10-04, FR-11, design §4.12).** A card may
> offer one next step: the model chooses it by id from options the server built from views the
> page serves, and gives a reason of at most 240 characters. The checks are structural only (an
> id outside the options, a step on a silent card, or a blank or overlong reason is refused);
> whether the reason is supported is owned by calibration and evaluation (ADR-082). Options and
> the `next_step` schema property (`insight-narration-v2`) are served only for a contract whose
> `next_steps.allowed_kinds` is non-empty, which happens only together with a gated prompt
> (R6-6); every contract ships `[]`. Served steps travel with continuation messages and are
> stored on the cache row; a legacy row has none.

### 2. Guardrails replace prohibition

> **Amended 2026-09-24 (product decision, same day as acceptance).** The verified-derivation
> mechanism below was prototyped and rejected before any of it was committed: policing prose
> numerics (token audits, entity masking, declared operands, exact server recompute) is too
> brittle — it refuses natural analyst language ("roughly 14%", "three of five campaigns")
> and turns good exploratory narrations into blank cards. The owner's principle replaces it:
> **the model reasons with the same liberty the chat agent has; the gate is label honesty,
> never evidence-boundedness of prose.** What remains deterministic, because it is cheap and
> unambiguous: the model's declared `cited_figures` list must contain only server-supplied
> finding values (the ADR-068 rule, now enforced for **every** output class instead of only
> the never-model-generated `observed` class), and internal wire tokens are rejected before
> caching. Prose figure-faithfulness and hypothesis-worded-as-fact are **calibration-owned**
> (decision 8), exactly as chat quality is owned by its scenario harness and judges — with
> the symmetric mandate that calibration must also hard-fail over-restriction: must-accept
> exploratory cases (labeled hypotheses, rounded figures in prose, interpretive readings)
> gate every prompt the same way fabrication cases do. Consequently the narration schema
> stays `insight-narration-v1` (no `derivations` field, no slot-contract schema flip), the
> deep-dive schema likewise drops its declared `derivations` field — its content stands as
> decision 1 describes (what stands out, labeled readings, discriminating evidence, plus a
> grounded citation list), and a field nothing verifies is never left declared — and
> the residual risk is accepted and stated: a model can write a wrong number in prose while
> citing correct values; the mitigations are the stronger model family, exactness-calibrated
> prompts, and the dialog's cited-figures provenance display.

> **Amended 2026-09-25 (product decision — customer-facing surfaces carry customer words
> only).** The cited-figures provenance display leaves the mitigation list: citations and
> every other internal stamp (prompt versions, cache vocabulary, gate names) no longer
> render anywhere customer-facing. The `cited_figures` declaration itself is unchanged and
> stays load-bearing — the deterministic subset-of-supplied-values gate still refuses an
> ungrounded generation before caching; only its display is withdrawn. Against the stated
> residual risk — a wrong number in prose beside correct citations, the one case the gate
> passes by construction — what remains is the stronger model family, exactness-calibrated
> prompts, and calibration ownership (decision 8's must-fail fabrication families);
> withdrawing the display removes the reader-side cross-check for that divergence, an
> owner-accepted trade. Dialog composition for this revision is owned by the design roster
> at `2026-09-25.1` and the insight-presentation contract.

- ~~**Verified derivation instead of "no arithmetic".**~~ Superseded by the 2026-09-24
  amendment above. The original text mandated server recompute of every model-derived figure
  from declared operands, a canonical figure representation shared by payload, citations, and
  a prose verifier, and a derivation-carrying successor schema; it is retained here struck
  through for the decision history only.
- **Exploration is labeled, fabrication is refused.** Candidate readings, contrasts, and
  what-to-check-next suggestions are the product and are carried by the existing output-class
  taxonomy (`Perspective`, `Hypothesis`). Hard refusals stay: external facts the platform has no
  data for (benchmarks, seasonality, market events), unsupported causal assertions stated as
  fact, dropped lag/estimate/basis qualifications, internal enum or code tokens (rejected before
  caching), and outputs whose only content is a restated tile.
- The slot evidence improvements from the 2026-09-24 prompt review (server-selected
  relationships, typed qualifications, distinct-measure gates) improve both tiers but no longer
  gate delivery: the card tier stays conservative and the deep tier receives full context with
  explicit epistemic framing.

### 3. Generation envelope migration

- Replace native structured output with **forced tool use** in `BedrockInsightGenerator` and the
  calibration adapter: one tool per schema (`insight-narration-v1` for cards — unchanged per
  the 2026-09-24 amendment to decision 2; a new `insight-deep-dive-v1` for the deep tier),
  `toolChoice` pinned, response read from the single `toolUse` block, all existing post-decode
  validation retained.
- Remove `temperature` from insight and calibration envelopes (refused as deprecated by the
  target models). Serve-stability continues to come from content-identity caching — each
  identity generates once — not from sampling parameters.
- Keep zero SDK retries and bounded transport timeouts; the deep tier gets its own, longer read
  budget sized during implementation against the request envelope.

### 4. Model configuration and staged Haiku retirement

- **Phase 1 (this decision's delivery):** introduce insight-specific model configuration
  (`ADLYTICS_INSIGHT_QUICK_MODEL_ID` / `ADLYTICS_INSIGHT_DEEP_MODEL_ID`, defaulting to the agent
  values; the runtime already reads insight-specific model ids — `ADL_INSIGHT_QUICK_MODEL_ID` /
  `ADL_INSIGHT_DEEP_MODEL_ID`, currently fed from the agent values by every insight stack — so
  Phase 1 adds the operator override and IAM, not a new runtime seam) and set them to
  `us.anthropic.claude-sonnet-5` / `us.anthropic.claude-opus-5`. The
  registry's existing `quick`/`deep` route vocabulary is unchanged; activation evidence must
  include a live structured-generation probe against both configured models.
- **Phase 2 (separate slice, same end state):** migrate the chat assistant harness, evaluation
  judges, and any remaining Haiku 4.5 / Sonnet 4.5 call sites to the new model family, with
  per-call-site envelope verification (each has its own `temperature` and format assumptions),
  scenario-harness evidence per the agent evaluation steering, and evaluator recalibration.
  Haiku 4.5 is removed from configuration when Phase 2 completes; the end state is owned by this
  ADR, the sequencing exists because Phase 2 failures would break surfaces unrelated to
  insights.

### 5. Horizon structure: each preset answers a different question

> **Amended 2026-09-25 (calibration evidence — slice H prompt authoring).** The
> `operational-pulse` role's model-level "bias toward silence" clause is retired. Calibration
> showed prompt-level re-screening suppressed slot-passed findings (metric-focus and
> returns-pressure returned silence on payloads whose values had already crossed the slot's
> materiality bar), so the shipped foundation instructs the opposite: the model's call is
> emphasis, never re-screening. The 1D screen is server-side only — this decision's
> horizon-scaled materiality in the slot contracts, whose per-horizon values are currently
> uniform. If 1D over-serving materializes, the remedy is tuning those thresholds and adding
> expected-silence calibration cases (finding C6 of the 2026-09-25 adversarial prompt
> review), never restored prose re-screening. The role keeps "prefer absolute changes."

> **Amended 2026-10-05 (unit-aware materiality with operand bases; R-1, R-18, R-26, R3-5,
> R5-13, R7-4, R8-4).** One threshold per horizon compared a 0.4 pp CTR move, a 400 USD spend
> move and an 8 % ROAS move against the same number. Materiality is now unit-, basis- and
> metric-aware:
>
> - **Seven finding units.** `currency`, `unit_price`, `count`, `rate_pp` (a rate change,
>   compared as `abs(value) × 100`), `multiplier`, `percent` (every percentage-change finding)
>   and `share` (every share-of-total finding). The metric dictionary decides each finding's
>   unit through its `units`, `kind` and `ratio_scale`; `FINDING_METRIC_DICTIONARY_IDS` in
>   `src/adapters/insight_finding_units.py` is the one map from the two artifact ids that are
>   not dictionary ids (`spend`, `attributed_roas`).
> - **Operand bases.** Each finding carries what it compares: `campaign_window`,
>   `portfolio_window`, `retail_window`, `product_window` or `campaign_day`. A contract
>   declares thresholds per basis, because a portfolio total moves far more than one
>   campaign's. `ALLOWED_FINDING_UNITS` declares, per slot, every (kind, metric, basis) its
>   builder emits; an undeclared triple, basis, unit or prior scale raises `InvariantError`
>   (fail closed), and the loader refuses a contract that lacks a threshold or bound for any
>   triple of a kind it requires.
> - **`per_metric` rows** (keyed by dictionary id) size one metric's amounts where one basis
>   compares amounts of very different size (impressions and purchases; CPC and CPM). They
>   apply to amount units only (`currency`, `count`, `unit_price`); a percentage or rate
>   finding on the same metric meets its unit row. Every `unit_price` metric has its own row.
> - **Near-zero prior bound per scale.** The bound is in the prior's own scale (`currency`,
>   `count`, `rate_pp` in points, `multiplier`), and per metric for `unit_price`, because a
>   CPC and a CPM differ by 1,000×.
> - **Share thresholds are pinned** at the earlier values (`portfolio-exposure` 1,
>   `detail-funnel` 0.01), so their outcomes do not change.
> - **Scale-free values are per basis.** On the aggregate bases (`portfolio_window`,
>   `retail_window`) they are the committed fixtures' pooled medians per horizon role, set on
>   2026-10-05 by `tests/unit/test_gate1_admission_band.py` (table in `validation/RESULTS.md`).
>   That test fails a row whose share of 56 window ends clearing its threshold leaves 20–80 %
>   on any preset, so it guards later edits only (R8-4).
> - **Limits.** Currency thresholds are absolute amounts in the account's source currency
>   (single-currency USD in dev), so another currency or another account's scale needs its own
>   values and its own admission table.
> - **Selection judges required kinds only.** A focus contract requires `absolute-change`, so
>   its percentage findings are context and are not gated; its `percent` rows and `unit_price`
>   bounds are declared for the materiality cases and for a later contract that requires them.
>
> Each contract also declares `materiality_cases` (expected admissions, silences, near-zero
> suppressions and not-built percentages), run in `make verify`. Implementation note: unit and
> basis are bound in `predicate_findings` through a `FindingSource`, rather than as new
> `change_findings` parameters, so the arithmetic helper stays unit-blind.

Insights remain generated per preset window (the cache identity already carries the window), but
the generation stops being horizon-blind:

- **Typed window profile in every payload:** window length and grain, the aligned-prior
  definition, and a closed horizon role — `operational-pulse` (1D: what changed yesterday;
  prefer absolute changes; bias toward silence), `tactical-review` (7D: this window against the
  aligned prior), `pattern-confirmation` (14D: consistency and offsets over single movers). The
  payload declares the role; interpreting it is prompt work (decision 6), never
  instruction-like context.
- **Horizon-scaled materiality:** slot contracts declare per-horizon thresholds; one unit-blind
  minimum no longer serves 1-day and 14-day windows alike.
- **Near-zero-prior guard:** percentage findings against near-zero priors are suppressed
  server-side (zero priors already are), so degenerate percentages never reach the model. The
  near-zero bound is slot-contract-owned, declared beside the per-horizon materiality it
  refines.
- **Cards stay single-window.** The window-scope integrity rule holds for the card tier
  unchanged.
- **Cross-horizon perspective lives in the deep dive only:** the deep payload may carry the
  sibling presets' direction as a typed, direction-only finding (no second-window figures),
  read from the same publish cohort's baked artifacts. This is a deliberate, narrow carve-out
  to the window-scope rule and is named in the Amends paragraph.

### 6. Prompt topology and composition

- **One prompt per slot per tier** — twelve card prompts and up to twelve deep prompts,
  activated slot-by-slot. Presets do not multiply prompts: each card prompt carries all three
  horizon roles as explicit sections and the payload's window profile selects the branch.
  Splitting a slot's prompt by horizon is an evidence-based escape hatch taken only when
  outcome calibration shows a horizon misbehaving under the shared prompt; the calibration
  scenario matrix (slot × horizon × case family) is where the horizon burden lives.
- **Composed at authoring time from git-sourced fragments.** One shared foundation, one core
  per tier, and one block per slot live as reviewed files in this repository; a deterministic
  renderer composes `foundation + tier core + slot block` into the complete prompt text, which
  ships through the existing create → calibrate → activate path. The registry remains the only
  store of the rendered, immutable text that runs; rendered prompts are never committed to git.
- **Fragment traceability:** each composed registry version records the digests of the
  fragments that rendered it, so a served narration traces narration → prompt version → exact
  sources. Runtime provenance is unchanged: the served slot and dialog keep stamping the exact
  `prompt_version`.
- **The operator edit loop survives.** ADR-069's create → calibrate → activate remains the only
  path any prompt reaches an environment, and Prompt Administration's direct create-version
  capability is retained for operator-authored versions — the no-deploy emergency loop. A
  direct version carries no fragment digests and its record marks it unsourced; composed
  versions are the norm, and source-to-narration traceability is qualified accordingly (it
  holds for composed versions, not for direct ones).
- **Foundation edits fan out by design:** one core change re-renders every dependent prompt,
  and each re-rendered prompt recalibrates before activation. Consistency becomes enforceable;
  the ship tooling batches the create/calibrate/activate loop under a bounded concurrency.

### 7. Performance, logging, and tracing

> **Amended 2026-10-13 (stage-1 trace vocabulary).** The governed generation span was
> renamed from `insight.generation` to `insight.model.call` so each exported child describes
> one provider call. Operational filters and the span contract use the new name.

- **Speed.** The card tier keeps its bake-time precompute and content-identity cache — the
  serve path stays a cache hit, and Sonnet 5 latency is paid at bake or first miss, not per
  view. The deep tier pays Opus 5 latency exactly once per clicked identity; the dialog opens
  instantly with card content and the deep section fills progressively, with the continuation
  queue absorbing generations that outrun the request envelope. Fragment composition orders
  shared text first (foundation, then tier core, then slot block), which makes the common
  prefix eligible for Bedrock prompt caching across slots within a bake batch — support on the
  configured models is an activation-time check, not assumed here. `maxTokens` is sized per
  tier. Cross-horizon direction findings are read at bake time from the same publish cohort,
  never fetched at serve time.
- **Latency budgets are declared and measured, not hoped.** Activation evidence for each tier
  must include measured generation latency (p50/p95 from the existing `insight.model.call`
  span) against a declared per-tier budget: the card tier inside the current serving budget,
  the deep tier inside a dialog budget the insight-presentation contract revision (the dialog
  slice's owning design doc) declares before implementation of that slice begins.
- **Tracing.** Generation for both tiers reports under the governed `insight.model.call` span,
  extended with tier, horizon role, and verification outcome attributes; the span keeps its
  single trace-boundary owner per the trace contract. Token usage and stop reason remain
  recorded before validation can raise, so failed generations stay diagnosable.
- **Logging.** The existing slot/batch operation logs gain tier, horizon role, fragment digest,
  and the verifier outcome (passed/refused/skipped, per the amended decision 2's slim gate) —
  bounded typed fields only, never model text. Calibration runs log per-scenario outcomes with
  the evaluator identity that judged them.

### 8. Outcome calibration with a fabrication-focused rubric

Prompt versions for either tier activate only through outcome calibration: fictional payloads in
the production envelope shape, held-out scenarios, deterministic hard failures first (schema,
grounded citation list, wire tokens, qualifications), then independently calibrated evaluator
dimensions per the evaluation steering. The rubric rewards exploratory value and attention
utility and hard-fails fabrication and tile restatement — it does not penalize interpretation.
Per the 2026-09-24 amendment to decision 2, calibration also owns prose figure-faithfulness
and label honesty, and the mandate is symmetric: the corpus carries must-**accept**
exploratory cases (a labeled hypothesis, rounded figures in prose, an interpretive reading),
so an over-restrictive prompt or check fails the gate exactly as a fabricating one does.

## Consequences

- Two new engineering seams before any prompt activates: the tool-use envelope migration
  (generator + calibration) and the deep-dive request path through the dialog slice (schema,
  cache tier, IAM for the new profiles, dialog fetch + pending states) — including a revision to
  the owner-signed insight-presentation contract: a filled card with a pending deep section is a
  new dialog state, and the dialog re-fetch is new behavior relative to today's passive
  generation-pending recovery.
- Deep-tier cost is bounded by clicks and caching rather than bake fan-out; card-tier cost moves
  from Haiku to Sonnet pricing and should be observed via the existing `insight.model.call`
  span usage fields after cutover.
- The prompt registry's calibration gate changes meaning: "passed" will attest generated-output
  quality, not summary classification. Existing version history remains readable.
- The registry gains a tier dimension (a card and a deep prompt per slot are distinct lifecycle
  identities) and version records gain fragment digests; slot contracts gain per-horizon
  materiality, the near-zero-prior bound, the window-profile declaration, and — for deep slot
  contexts — the cross-horizon direction input declaration; the `insight.model.call`
  registration in `contracts/observability/spans.yaml` gains the tier, horizon-role, and
  verification-outcome attributes.
- Bake-time generation cost rises with the horizon-aware payloads and, later, deep-tier
  adoption; both are observable per generation through the extended span usage fields. Where
  the activation-time prompt-caching check passes, the shared-prefix ordering bounds
  input-token growth across a bake batch; where it does not, input tokens grow with batch size
  and the measured span usage is the evidence for any follow-up decision.
- Until Phase 2, the assistant and evaluators continue on their current models; the platform
  runs two model families deliberately, recorded here rather than discovered.

## Rejected alternatives

- **Keep native structured output and choose different models.** The owner's model direction is
  explicit, and the target models refuse the parameter; tool-use forcing is the documented
  portable mechanism and verified working against both models.
- **Deep model at bake time for every card.** Multiplies the expensive model across every
  preset, window, and slot regardless of interest; the click is the natural meter.
- **Prohibit arithmetic harder.** Already demonstrated ineffective; prohibition without
  assurance produced unverified arithmetic that prompts alone never caught. The adopted
  position (decision 2 as amended 2026-09-24) beats both prohibition and brittle prose
  policing: grounded citations and wire-token rejection stay deterministic, while
  calibration with fabrication hard-fails and the dialog's cited-figure provenance carry
  the assurance — with the residual prose risk accepted and stated rather than ignored.
- **Repoint the shared agent model config directly.** Couples an insights decision to assistant
  and evaluator behavior in one deploy; staged migration keeps each surface's envelope
  verification honest.
- **One prompt per slot per preset (×3 topology).** Triples authoring, calibration, and
  lifecycle for differences that are mostly data (thresholds, windows, guards); the horizon
  role is the only prompt-shaped difference and a typed payload enum selects it.
- **Runtime prompt composition from registry fragments.** Individually versioned fragments can
  preserve request provenance as a tuple, but the calibratable unit is the composed whole: any
  safe design must freeze and calibrate each tuple before it serves, which is build-time
  composition with extra serve-time machinery — plus its one distinctive property, foundation
  edits silently changing calibrated behavior without recalibration, is exactly the property
  the gate exists to prevent.
- **Streaming deep-dive delivery.** Deferred, not adopted: the pending taxonomy, dialog
  re-fetch, and continuation queue already deliver progressive arrival without a streaming
  envelope; revisit if measured deep-tier latency makes the non-streamed fill unacceptable.
- **Speculative deep-dive prefetch (on hover or card render).** Defeats the click-metered
  economics that justify the expensive model; the cache already makes every repeat open free.
