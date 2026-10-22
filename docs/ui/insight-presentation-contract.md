
| | |
|---|---|
| **Type** | Shared UI interface concept |
| **Status** | Design baseline, revision `2026-09-23.4` (insight cards never share a grid row with metric tiles), accepted 2026-09-23; the served vocabulary it renders is implemented and live (ADR-068 amendment, 2026-09-23), and the frontend rendering slice is implemented and live-verified 2026-09-24 against this revision. §3.1 (the deep-dive dialog section) is declared 2026-09-24 per ADR-076 and its serving slice is implemented and live-accepted 2026-09-25; its approved latency-budget line carries the deployed confirmation and the 2026-09-25 first-open latency disposition. |
| **Owns** | The insight slot card: its five designed states, the mandated treatment per state, the clickable card affordance, and the insight-focus dialog composition per state |
| **Parent** | [Decision Workspace UI](README.md) |

## 1. Purpose and boundary

Every declared insight slot renders as a card, and **no card is ever an unexplained blank**
(the declared-outcomes rule). A slot that carries no narration names its absence class and a
governed customer-legible reason, both **served** — the browser never composes, rephrases, or
maps a reason from a code. One distinction governs all treatments:
*couldn't-evaluate is loud; evaluated-and-declined is quiet but discoverable.* Shouting every
materiality silence would train customers to ignore the callouts.

This contract owns the shared card grammar across the three pages (Campaign Portfolio,
Campaign detail, Retail health) and the insight-focus dialog those cards open. Each page LLD
owns its slot roster and layout. The served payload vocabulary is owned by
[ADR-068](../decisions/adr-068-fill-declared-insight-slots-with-direct-structured-model-calls.md)
(2026-09-23 amendment): wire states `filled | silent | unavailable`, plus the absence pair
`absence_class` + `absence_reason`, present exactly when an absence is declared.

## 2. The five designed card states

| Served state | Card treatment | Tag (contract-owned label) | Source of the body words |
|---|---|---|---|
| `filled` | Narration, evidence line — as today. Clickable. | The output class (`Observed` / `Perspective` / `Hypothesis`), shown as "Fact" / "Reading" / "Idea to check" ([glossary](../glossary.md#customer-labels)) | Served narration and evidence line |
| `silent` + `evaluated-quiet` | **Quiet but discoverable.** Dashed border and quiet tag; the body states the served reason ("No campaign moved against the portfolio's direction enough to call out in this view.") in full-strength body text — quietness comes from the treatment, never from graying the words. Never an error or warning style. Clickable. | `Nothing noteworthy` | Served `absence_reason`: the customer string for why the slot went quiet (missing input, missing comparison, near-zero prior, nothing large enough), or the model-silence string; never the contract's silence condition (ADR-068, 2026-10-05) |
| `silent` + `ineligible` | **Explicit callout at the display site.** Warning-toned border; body states the served reason naming the missing evidence ("This insight requires a governed delivery follow-up read, which is not available for this view and window."). Clickable. | `Needs data`, shown as "Not enough data yet" | Served `absence_reason` (governed missing-evidence sentence) |
| `unavailable` + `generation-pending` | **Honest and calm.** Body states the served reason ("This insight could not be generated just now; it retries automatically and appears on a later view."). Not an error style — this is a declared healing state. Clickable. | `Retrying`, shown as "Loading" | Served `absence_reason` |
| `out-of-slice` | Never rendered: this class marks slots a composition deliberately does not generate, and such compositions do not render insight rows. A card receiving it anyway renders as evaluated-quiet treatment with the served reason. | `Nothing noteworthy` | Served `absence_reason` |

Rules every state shares:

- The body text is the served `absence_reason` verbatim. No internal code, enum token, or
  class name ever reaches the page: for a slot served with the absence pair,
  `absence_class` selects the treatment and its contract-owned tag label above, which are
  the only browser-side words that card carries.
- A **non-filled** slot served without the absence pair (a retained pre-taxonomy body)
  renders the evaluated-quiet treatment with this contract's one owned fallback line,
  "Nothing noteworthy this window." — it must never render blank. (Filled slots also carry
  no pair; they render their narration as normal.)
- State changes ride the window-invalidation contract in [Decision Workspace UI](README.md):
  a preset change re-resolves every card.
- **Placement:** an insight card never shares a grid row with fixed-shape metric tiles.
  Narration height is nondeterministic, so a card inline with a tile grid skews the row
  and shifts the tiles when narration arrives. Cards live either in a dedicated insight
  row (Campaign Portfolio) or as a full-width row below their section's tiles
  (Retail health), with a reserved minimum height so arrival never causes layout shift.
- **Uniform geometry:** insight cards follow the workspace one-row-per-card-group rule
  ([Decision Workspace UI](README.md) §5: one full-width row of equal columns, equal
  stretched heights, wrapping by viewport only), with two insight-specific additions.
  Less content never means a smaller card: an absence card occupies exactly the shape of
  a filled card, with its tag at the top and its served reason in the body position the
  narration would hold. And card body text — narration and served reasons alike — is
  clamped at five lines so one nondeterministic body cannot blow out the row; the card's
  insight dialog always carries the full text, so clamping never hides words the customer
  cannot reach.
- The metric-focus dialog's prompted interpretation slot renders its absences under this
  contract too: the served reason verbatim with the treatment its served class selects,
  superseding the generic fallback line the metric-focus contract previously mandated for
  that slot. The [metric-focus contract](metric-focus-contract.md) keeps ownership of the
  dialog's composition; this contract owns absence rendering wherever an insight slot
  appears.

### 2.1 What a Gate-1 evaluated-quiet card means (materiality, 2026-10-05)

A Gate-1 `evaluated-quiet` card means every supplied finding of a kind the slot requires was
judged and none was material. Each finding is judged in its own unit and against what it
compares: a rate change in percentage points, a ROAS change in times, a spend change in the
account's currency, and one campaign's window against a campaign row, never against the
portfolio's. A percentage change off a near-zero prior is silenced the same way, so a
"+900 %" off a vanishing base never reaches a card. The thresholds, units, bases and bounds
are the slot contracts' and the rule is
[ADR-076](../decisions/adr-076-tier-insight-narration-across-card-and-deep-dive-models.md) §5
(2026-10-05 amendment); the generated
[insight deterministic matrix](generated/insight-deterministic-matrix.md) projects every
slot's values. How a quiet card labels the values it evaluated is FR-8's, not this section's.

### 2.2 What a portfolio card reads (scope, 2026-10-05)

A portfolio card is filled from the served (sort, segment) segment: up to 50 of the
portfolio's campaigns, beside totals over all of them. So its model input names that scope
in the words the table uses:
- **Contribution** carries "campaigns in view" ("These are the 50 campaigns ranked 1 to 50
  by ad spend, highest first in this window, of 60 in the portfolio", or "These are all N
  campaigns in the portfolio, ranked by …"), the spend change in view and outside it, the
  in-view share of the portfolio's spend change (only when both moves share the total's
  sign, the share is at least 25 % and the total moved at least 3 % of its prior; both
  floors are uncalibrated), and "Among these N: U rose, D fell, F were flat".
- **Divergence** carries the "campaigns in view" sentence only, so a count it states is
  never read as the portfolio's.

The sort words are the table's own (`CAMPAIGN_SORT_FIELD_WORDS`, checked against the table's
labels by a cross-language test). These entries are model input, not rendered text.

### 2.3 Card next steps (FR-11, 2026-10-05; shipped dormant)

Every model card also reads three context entries: the comparison window in customer words
("…, the 7 days before this view"), a `data sufficiency` sentence (complete, or the gaps the
page already serves), and, on retail cards whose rows are every product in view, every
product's change with the dialog's summary lines. A card may offer one next step, chosen by
id from server-built options naming views this page serves (`src/adapters/insight_next_steps.py`
owns the kinds, labels and quotas). The options and the `next_step` schema property exist
only when the slot's contract lists `next_steps.allowed_kinds`; every contract ships `[]`, so
no card offers a step until the gated prompt and the populated lists ship together.

**Slot wire grammar, `NextStep.v2`** ([ADR-122](../decisions/adr-122-declare-next-step-targets-as-a-typed-union-checked-against-navigation.md),
Campaigns render format 9, Retail health render format 7). A filled card with a step carries
`next_step: {kind, label, reason, target}`; every other slot omits the key, the same exact-shape
rule as the absence pair, so retained artifacts re-attest byte for byte. `target` is one closed
object per kind, and an optional key is absent, never null:

| Kind | `target` |
|---|---|
| `open_page` | `page_id`, optional `preset_id`, `filter_id`, `section_id`, `highlight_channel`, `focus_campaign_ref` |
| `open_preset` | `page_id` (the card's tab), `preset_id` |
| `open_campaign_detail` | `page_id`, `campaign_ref`: the capability this response minted for the campaign's row |
| `open_product_detail` | `selection_reference` (decoded; no server step until Retail product detail) |
| `open_metric_focus` | `page_id`, `metric`, optional `filter_id` |
| `open_evidence_status` | `null` |
| `ask_assistant` | `question`: 1 to 240 characters, server-written and dated with the window label |

- **Declared targets.** `domain/presentation/next_steps.py` checks every target as the options
  are built: the page must be a routed tab (a v2 page entry with `navigation`), and its preset,
  filter and section must be declared there. An undeclared one fails, never drops. No page
  declares filters yet, so no step can name one. `open_page` steps come from the page that
  serves them, never from a card's model. The targets come from the card's served surface,
  which no derivation builds yet, so the contract loader refuses a card contract that lists a
  kind ([delivery slices](information-architecture/delivery-slices.md), Slice 5).
- **Keys cached, capabilities served.** The cache row and the continuation message hold a
  campaign step's `campaign_key`, never sent. The serve path maps it to the capability it
  minted for that campaign's row in the same response, or drops the step. It also drops a step
  whose `filter_id` the workspace doesn't offer. The wire codec refuses a key, and the cache
  codec a capability.
- **Questions.** A question that isn't dated with the window label, states a cross-channel
  attributed total, or uses a causal verb about a full-funnel or frequency figure isn't offered.
- **The browser.** It renders the reason as text and the label as one chip described by it. A
  same-tab step needs this page's handler and a target on this tab: the preset control (an
  available preset only), the coverage sheet (only with a block), the campaign row, or the
  metric focus dialog (a metric it can focus only). A cross-tab step is a link to a tab in the
  session's manifest, and only when the URL carries its whole target: one naming a section, a
  highlighted channel or a campaign renders nothing until the URL can. The browser counts the
  240-character bounds in code points, as the server does. "Ask the
  assistant" closes a dialog it sits in, opens the assistant, puts the question in the composer
  and moves focus there; it sends nothing. A step this page can't reach renders no control,
  never a dead one.

### 2.4 The coverage block (`EvidenceStatus.v2`, 2026-10-07)

[ADR-119](../decisions/adr-119-serve-window-coverage-as-one-server-written-evidence-status-block.md)
(Proposed). Campaigns (render format 8), Retail health (render format 6) and campaign detail carry
`evidence_status`, written by `domain/presentation/evidence_status.py`:

| Field | Meaning |
|---|---|
| `version`, `window`, `summary` | `2`, the page's window, and the sheet's opening sentence |
| `entries[]` | One per figure and role, in the page's order: `slot`, `role`, `label` and `role_label` (dictionary labels), `state` (`complete`, `missing_days`, `partial_days`, `unavailable`), `missing_dates` (ISO), `missing_text`, `reason`, `recovery`, `absence_text` and `coverage_text` |

- **Typed absence** ([ADR-118](../decisions/adr-118-render-every-page-from-one-set-of-shared-patterns-and-extended-tokens.md)
  decision 5). A `missing_days` or `partial_days` figure withholds its change: its comparisons
  carry `absolute_change`, `percentage_change` and `change` all null beside both values. The
  chip's place reads `absence_text` ("1 day not yet sent.", "1 partial day.") with "See why:
  {figure}", and the basis reads `coverage_text` ("6 of 7 days"). Decoders on both sides refuse a
  withheld change the block doesn't name, and a named figure that still carries a change. An
  undefined ratio (ADR-087) has no change to withhold.
- **Stacked rows.** A stacked table cell that is a typed absence ("—") drops the coverage part of
  its basis line (`stackedBasis` in `frontend/components/DataTable.tsx`).
- **The sheet.** "Coverage for this window" renders only the block, with no browser label map.
  `missing_text` names the missing days where the server knows them. A figure refused because
  no publication has arrived for its period (missing current or comparator publication, or
  never loaded) reads "Not sent". A complete figure, and a refusal that isn't about days
  (suppressed, truncated, incompatible scope, series divergence), reads "None".
- **Every body carries the key.** A Campaigns or Retail health body carries `evidence_status`:
  the block on a complete body, `null` exactly on an empty or insufficient one. Campaign detail is
  written at serve time, so it always carries the block. The block-less read of a Campaigns body
  before format 8 or a Retail health body before format 6 retired on 2026-10-09 (owner revision
  of 2026-10-08): both sides refuse it.
- **Bakes.** A baked window resolves inside common source coverage and an answer with a hole is
  refused, so a bake serves every figure `complete`. The missing-day and partial-day states are
  unit-proven with fixtures until a materializer serves partly reported days.

## 3. Clickable cards and the insight-focus dialog

Every insight card is clickable, exactly like the metric tiles: an invisible full-card button
whose accessible name is "Open the insight view for {purpose}", focus-visible ring, Escape
returns focus to the opening card. The dialog reuses the metric-focus visual language (scrim,
inert page, ‹ › hops between the page's insight slots without closing) and renders per state:

| Card state | Dialog body |
|---|---|
| `filled` | The narration and its evidence line, full width; the deep band beneath it (§3.1); the provenance strip. Citations never render (2026-09-25 amendment below). |
| `evaluated-quiet`, Gate-1 (`prompt_version = deterministic-gate1`) | The rule that held: the served reason plus the contract's evaluated numbers against their materiality threshold, rendered as labeled values. |
| `evaluated-quiet`, model-declined (real prompt version) | The served reason plus the deterministic findings the model evaluated and declined to narrate. |
| `ineligible` | The served reason, with each missing evidence input as a labeled row, and the nearest available alternative (a preset or view where the slot is eligible) when the server names one. |
| `generation-pending` | The served reason and nothing speculative; no manual retry control — recovery is automatic and declared. |

**Provenance strip (every state — amended 2026-09-25; design revision
`2026-09-25.1`):** one quiet customer-word line at the dialog foot, replacing the earlier
side-rail provenance block. Nothing internal renders: no `prompt_version`, no cache
vocabulary, no gate names. The wire's non-model `prompt_version` stamps select contract-owned
customer lines: `deterministic-gate1` reads as "Decided by a fixed rule from the values on
this page · no model involved"; `deterministic-v1` reads as "Computed directly from the
values on this page · no model involved"; `not-generated` reads as "No reading was generated
for this view"; every other stamp (a real registry version) reads as "One governed reading of
this view · updates when this page's data does". The dialog is single-column: the served words full
width, the deep band full width beneath them (filled cards only), the strip at the foot.

**The dialog's on-request read — shipped for filled cards, deferred for the rest:** the
deep band for filled dialogs (§3.1) ships and serves live (2026-09-25, ADR-076 Phase 1
slice G; server-computed on request, cached under the card's identity plus tier). The
remaining on-request additions stay deferred by decision and render today as the served
reason plus the provenance strip: the evaluated-numbers strip for quiet dialogs, the
itemized missing-evidence rows for ineligible dialogs, and the nearest-eligible
alternative when the server can name one. When they ship they are server-computed each
serve (the metric-focus dialog's pattern); none are baked into the artifact, and the
browser composes none of them.

### 3.1 The deep-dive section (dialog revision 2026-09-24, discharging the ADR-076 reservation)

[ADR-076](../decisions/adr-076-tier-insight-narration-across-card-and-deep-dive-models.md)
(accepted 2026-09-24) extends the dialog slice's on-request read beyond deterministic
recompute: opening a **filled** card's dialog also requests a deep-dive analysis generated by
the deep-route model, cached per the card's content identity plus a tier discriminator and
the deep prompt's own version. Since 2026-10-05 both identities also name their input (the
card's input digest, and the deep-input revision; ADR-076 decision 1 owns the rule). This
section owns the designed behaviors; the deep serving
slice implements them and may not start before the budget below is declared.

- **Composite state — filled card, deep section pending.** The dialog opens instantly with
  everything §3's `filled` row mandates (narration, evidence line, provenance strip);
  beneath it, the deep section renders under the shared skeleton discipline — with the
  staged preparation captions §3.1 declares below — while its generation runs. This
  composite is a designed presentation of `filled`, not a sixth card state: the card
  taxonomy is unchanged, and non-filled dialogs never carry a deep section.
- **Deep section content (`insight-deep-dive-v1`) — composition amended 2026-09-25 (design
  revision `2026-09-25.1`).** A full-width band beneath the dialog's
  evidence: the standout and each candidate reading render as sibling cells that fill
  each row evenly on wide viewports — two cells two-up, three three-up, the four-cell
  maximum two-by-two, never an orphan cell — and stack on narrow ones, with "What would
  tell these apart" beneath, so the band's variable length extends the dialog evenly
  instead of skewing a column. Up
  to three candidate readings, each labeled as a reading, never as a finding. Figures
  follow [ADR-082](../decisions/adr-082-govern-the-agent-by-evaluation-and-audit-not-text-heuristics.md)
  (2026-09-27): prose may state the supplied values and figures validly derived from them,
  and no text check refuses it. The declared citation list stays in the stored and served
  deep shape, is not verified, and **never renders** — the section speaks customer words
  only, with no citations, prompt versions, or cache vocabulary. Figure faithfulness and
  label honesty are owned by calibration and evaluation, the same liberty and assurance the
  chat agent has.
- **Deep composition and scope labels (2026-10-05).** The deep payload is the card's
  findings and context plus the deep supply ADR-076 §1 (2026-10-05 amendment) owns. On a
  portfolio page slot the deep section reads one served segment of rows beside totals over
  the whole portfolio, so its inputs say which is which: row-built entries are labelled
  "(campaigns in view)", totals "(whole portfolio)", and a "campaigns in view" sentence
  names the segment ("These are the 10 campaigns ranked 51 to 60 by ad spend, highest first
  in this window, of 60 in the portfolio"). The labels are model input, not rendered text.
- **Deep request grammar (W11, 2026-10-05).** `POST /api/presentation/insight-deep-dive`
  takes `session_id`, `slot_id`, and then one of: `temporal_selection` (parent scope);
  `metric` (focus slots); or `selection_reference` (campaign detail, with no reporting
  period). A portfolio page card is filled per served (sort, segment), so its deep request
  also carries `sort` (the page body's `{field, direction}`) and `segment` (sent only above
  0), parsed by the page body's own parsers; the server resolves that same segment, so the
  deep section and its card share one artifact revision. Every other slot, focus included,
  refuses `sort` or `segment` with 400 `invalid_fields`.
- **Pending and re-fetch.** A generation that outruns the request envelope returns the
  designed pending treatment (served reason, no speculative content, no manual retry) and
  the continuation queue carries it; the dialog then **re-fetches** on a bounded, declared
  schedule while open — today's passive generation-pending recovery becomes active only
  inside an open dialog, and closing the dialog stops the polling. *Declared 2026-09-24,
  before the deep serving slice began:*
  the dialog re-fetches a pending deep section every **5 seconds**, bounded at **6
  re-fetches** (30 seconds of polling per open); the bound covers the approved p95
  budget below twice over, and the interval keeps a typical continuation fill (p50
  ≈ 9 s) to two polls. After the bound the pending treatment persists without further
  polling; closing and reopening the dialog starts a fresh bounded cycle, which is a
  cache hit whenever the continuation has completed meanwhile. A repeat open of a
  completed identity is a cache hit and renders complete immediately.
- **Deep failure is quiet, never blocking.** A deep section whose generation ends in a
  typed failure renders its served reason in the section's designed absence treatment; the
  card content above stays fully usable. The dialog never blocks on the deep tier.
- **Latency budget.** The deep section's declared p95 fill budget must be declared and
  approved here before implementation of the deep serving slice begins (ADR-076 §7).
  *Approved 2026-09-24:* **p95 ≤ 15 seconds** for
  the deep section fill, with the pending taxonomy absorbing overruns. Basis: twelve
  operator-credential Opus 5 generations in the production deep envelope shape (forced
  `insight-deep-dive-v1` tool, full-slot-context fictional payload, dialog-bounded analysis
  length) measured p50 8.7 s / p95 12.4 s / max 12.9 s with every run completing
  `tool_use`; the 15 s proposal leaves ~20% headroom for larger production payloads and
  provider variance. An unbounded-length envelope instead runs to the token ceiling
  (~23 s truncated), so the deep prompt authored in slice H must bound the analysis shape
  exactly as the dialog renders it — this measurement is contingent on that bound.
  *Deployed confirmation (slice G live acceptance, 2026-09-25):* production-envelope Opus
  generations on a metric-focus-sized roster measure 14.2–16.8 s and can exceed the request
  path's 20 s transport on full evidence rosters. Cached and synchronously generated fills
  hold the 15 s p95; a first open that defers to the durable continuation completes in
  ~9–26 s observed, absorbed by the pending taxonomy exactly as declared (a reopen serves
  from cache).
  *Disposition (2026-09-25):* the first-open latency is accepted as-is, so the deep
  render stays observable rather than budget-clipped, with two UX levers
  applied the same day: the deep model's adaptive-thinking effort is capped at `medium`
  for the deep tier only (validated live: default-effort runs measured 15–37 s with one
  token-ceiling truncation, medium 13.6–16.3 s with none; the card tier keeps the model
  default), and the loading state renders deterministic staged preparation captions (a
  fixed five-stage roster advancing every 4 s and holding on the last) so a long first
  open reads as progress rather than a stall. The captions mirror what the pipeline
  really does without claiming live progress. The card-side open (everything above the
  deep section) stays inside the page's existing serving budget, unchanged.
- **Movers dialogs carry no deep section.** The two deterministic slots declare no deep
  tier (`deep: null` in their contracts); their dialogs keep today's scope exactly.

## 4. Acceptance

- Rendered live against real data: every non-filled card on every page and preset states its
  served reason; zero unexplained blanks (the hydration probe enforces the payload half
  continuously).
- Axe passes on the live populated pages and on the rendered dialog (heading order, focus
  return, inert background), per the repository's existing dialog acceptance.
- The loud/quiet distinction is visually verifiable in the mockups: an ineligible callout is
  noticeable at a glance; an evaluated-quiet card recedes next to a filled one.
