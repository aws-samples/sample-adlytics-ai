# ADR-002 — Canonical data model

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-17 |
| **Decision** | **Conform the dimensions and the metric semantics. Do not conform the facts.** A canonical layer sits between curated tables and tools; tools depend on it exclusively and never name a source table. |
| **Related** | [data-layer](../capabilities/data-layer/README.md) · [architecture](../architecture.md) · [ADR-039](adr-039-assembled-context-replaces-the-declared-prompt.md) · [ADR-052](adr-052-separate-ecosystem-platform-channel-and-ad-product.md) |

> **Source correction, 2026-08-24.** The decision to conform dimensions remains accepted, but one
> source premise below was wrong: [verified 2026-08-19] AMC `campaign` and `advertiser` names are
> outputtable at the LOW threshold. They can feed the campaign dimension directly; no external
> campaign-metadata source is required. [Sources/AMC](../sources/amc.md) §4 owns the evidence and
> [canonical model](../canonical-model.md) owns current fields. Historical wording below is amended
> where it would otherwise instruct implementation.

## Context

The bootstrap brief states a design principle: *"Channel-agnostic canonical model (designed to extend beyond Amazon Ads)."* That is the right instinct, but "canonical model" describes an outcome rather than a technique, and the techniques differ enough that picking the wrong one is expensive to undo.

The requirement, stated concretely: a user should be able to ask *"where is my spend going and what is it returning?"* and get one coherent answer, today across Amazon Sponsored Products / Brands / Display / DSP and Vendor Central retail, and later across other retail media platforms — without rewriting the tool layer each time a source is added.

### The constraint that rules out the obvious approach

The instinctive reading of "canonical model" is a single conformed fact table that everything lands in. That does not survive contact with AMC.

AMC output grain is **fixed at query-authoring time, inside AMC SQL**, because of the privacy thresholds. There is no `SELECT *`, every query must aggregate, and `user_id` can never be output. We receive ten pre-aggregated outputs at ten different grains with largely disjoint metric sets, and **we cannot re-cut them downstream**. Re-graining means re-authoring AMC SQL and waiting for a fresh execution.

So a design that depends on deriving a canonical fine-grained fact table from AMC is not merely awkward, it is impossible. Any canonical model here has to be one that tolerates facts arriving pre-shaped and non-uniform.

A second constraint: **AMC ad-attributed conversions and Vendor Central retail sales are different measurement systems and will never reconcile at any grain.** Amazon's own documentation says as much — the ad tables are advertising-pipeline sourced and subject to restatement and augmentation, while retail purchases come from the retail pipeline. A canonical model that puts both under one `revenue` column is not integration, it is a bug with good branding.

## Options considered

### Option 1 — Conform the facts (single physical canonical warehouse)

One set of conformed fact tables. All sources transform into them. Tools query only those.

Rejected. AMC's ten fixed grains would have to be flattened into a shared grain, which either throws away the aggregation that makes each output valuable, or produces a very wide sparse table where most columns are null for most rows. And since AMC cannot be re-grained downstream, an incorrect canonical grain is not a refactor, it is a re-authoring exercise across ten workflows. This option buys the least and costs the most.

### Option 2 — Semantic layer only (canonical meanings, source-shaped storage)

Keep curated tables at source shape. Define canonical entities, metrics, and dimension keys purely as a logical layer. Adapters map canonical requests onto whichever physical table can answer them.

Close to right, and this is the half that carries most of the value. The weakness is that with nothing physically conformed, every cross-source question becomes a join written in adapter code against differently-keyed tables. Channel-agnosticism ends up asserted in a document and quietly violated in mapping logic, which is exactly how canonical models rot.

### Option 3 — Conform dimensions, keep facts at natural grain, define metrics once ✅

Conform physically the things that must be shared (dimensions, keys, units, timezone, currency, measurement basis). Leave facts at whatever grain their source permits. Define each metric exactly once, as a repo artifact both code and prompts read.

**Chosen.** Reasoning below.

## Why option 3

**Integration comes from conformed dimensions, not from one big fact table.** This is the durable insight from dimensional modelling and it fits our constraints unusually well. Facts can live at different grains and still be compared, sliced, and narrated together *provided* they share dimension keys. Since AMC dictates our fact grains and we cannot change them, conforming dimensions is the only lever we actually control — and it happens to be the one that matters.

**It matches what "channel-agnostic" actually requires.** The question "spend by channel" needs `channel` to mean one thing and `spend` to be defined one way. It does not need one platform's impressions and another's in the same physical row. Adding a platform later means: new contracts, new mappings into the existing dimensions, no tool changes. That is the extensibility the brief is asking for, and option 3 delivers it without option 1's cost.

**It keeps the 6 MB tool-response ceiling and Athena scan costs manageable.** Narrow purpose-built aggregates are cheaper to scan and easier to fit in a response than a wide generic fact table. The architecture pushes us toward option 3 for operational reasons too.

**It gives the former context-assembly design something real to describe.** Canonical metric definitions as a versioned artifact could be generated rather than hand-written in a prompt, preventing the prompt's definition of ROAS drifting from the curation job's.

## What the canonical model contains

### 1. Conformed dimensions

| Dimension | Canonical keys | Notes |
|---|---|---|
| `channel` | governed platform/channel hierarchy | the extensibility hinge; source labels never become canonical keys by copying |
| `product` | `amazon_asin`, `marketplace_key`, SCD2 validity | marketplace-qualified natural identity; hierarchy and brand come from Data Kiosk |
| `campaign` | canonical campaign key plus effective-dated source aliases and names | AMC supplies the source key, campaign name, advertiser key, and advertiser name |
| `date` | `date_key` plus source business-day semantics | pre-aggregated source days retain their source timezone |
| `marketplace` | `marketplace_key` plus qualified source aliases | source IDs, names, and country codes resolve explicitly rather than by equality |
| `currency` | ISO 4217 plus per-column source scaling | a divisor belongs to its source column, never to a platform or table |

### 2. The conformed media taxonomy — where source vocabulary must not leak

Today's source values are source-native. If those strings reach the tool layer as canonical keys, the
platform is source-specific no matter what the design doc claims. Accepted [ADR-052](adr-052-separate-ecosystem-platform-channel-and-ad-product.md)
completes this accepted mapping principle with five separate axes:

```
ecosystem      amazon | google | <further business ecosystems>
platform       amazon_ads | amazon_retail | google_ads | <further services>
channel_group  search | display | video | audio | offsite
channel        amazon_sponsored_products | amazon_dsp | <platform channel>
ad_product     <source buying or serving product mapped independently>
```

Source-native values map through one governed taxonomy. Tools use only axes a metric declares;
source system, measurement basis, marketplace, and commerce scope remain separate.

### 3. Canonical metrics

One definition each, versioned, in `contracts/shared/metric-dictionary.yaml`, consumed by curation, tools, ML jobs, and the Context Engine. Each entry declares: formula, inputs, units, valid grains, valid measurement bases, and known caveats.

The failure mode this prevents is specific and common: the curation job, the forecasting job, and the narration prompt each end up with a slightly different ROAS, and nobody notices until two answers in one conversation disagree.

### 4. Measurement basis — first-class, not a footnote

```
ad_attributed    from AMC; within an attribution window; restates for ~30 days
retail_actual    from Vendor Central; net of returns; restates as returns arrive
modeled          Amazon-modeled conversions; sub-ad-line dimensions are null
```

Every fact row carries one. Tools must never sum across bases, and the narration layer must name the basis when reporting revenue. This is the guardrail against the most damaging error available to this platform: presenting an ad-attributed figure as though it were actual sales.

## Where it lives

Current implementation locations are:

```
contracts/amc/                  governed AMC source mappings and producer queries
contracts/vendor-central/       governed Vendor/Data Kiosk source mappings
contracts/v2/source-packs.json  source-qualified interpretation and canonical publication roles
contracts/shared/metric-dictionary.yaml
                                one executable definition per metric
contracts/generated/tables.json generated physical canonical table inventory

src/domain/semantics/           typed identity, compatibility, candidate and publication rules
src/domain/measurement/         metric planning and deterministic arithmetic
src/adapters/outbound/athena/   checked-plan SQL composition and execution
```

Accepted [ADR-052](adr-052-separate-ecosystem-platform-channel-and-ad-product.md) adds one
governed taxonomy artifact during implementation. This ADR does not reserve any additional
filenames for artifacts that do not yet exist.

The rule that makes it hold: **nothing in `src/domain/`, no tool, and no scheduled job references a contract-specific table name.** That resolution happens in the adapter. It is checkable in CI alongside the existing `import-linter` boundary gate.

### The scheduled jobs are inside this rule, not outside it

Worth stating explicitly because it is the easy thing to get wrong. The forecasting and saturation-fitting jobs read through the canonical model like everything else. They are batch, they are infrastructure-shaped, and they sit in `adapters/jobs/` — none of which exempts them.

If the forecasting job computes ROAS from curated tables using its own logic, it drifts from the definition the measurement tool reports. The symptom is a forecast that contradicts a historical figure **within the same conversation**, where both numbers look plausible in isolation and neither is obviously the wrong one. That is precisely the class of error this ADR exists to prevent, and a batch job is a more likely place for it to originate than a tool, because nobody is watching a nightly job's arithmetic as closely.

Two specific obligations follow: the jobs consume metric definitions from `contracts/shared/metric-dictionary.yaml` rather than reimplementing them, and they respect `measurement_basis` — a model trained on ad-attributed revenue and one trained on retail actuals answer different questions and must not be blended into a single series.

## Consequences

**Accepted:**

- A mapping layer exists and must be maintained. Adding a contract means adding its mapping, not just its table.
- Some cross-source questions need a join at query time rather than reading one pre-joined table. Mitigated by materialising a small number of genuinely cross-channel daily aggregates (spend, impressions, clicks, attributed revenue by channel × campaign × date × marketplace) once access patterns are known — deliberately deferred until we have evidence, rather than guessed now.
- Canonical metric definitions need business sign-off, not just engineering agreement.

**Rejected consequence:** we are *not* promising that AMC and Vendor Central figures reconcile. The canonical model makes the difference explicit and legible instead of hiding it.

## What we need agreement on

These are business decisions with engineering consequences, and they belong to whoever owns the product definition:

1. **What counts as spend.** Media cost only, or media plus fees? AMC exposes audience fees and bid prices separately.
2. **Which revenue is the default.** When a user asks "what did my advertising return", is the answer ad-attributed revenue (AMC) or retail sales (Vendor Central)? They differ, both are defensible, and the default shapes every ROAS figure in the product.
3. **Advertised versus brand-halo.** AMC's `tracked_asin` includes same-brand products we did not advertise. Including halo makes ROAS look better. Which is the default, and is the other available on request?
4. **Attribution window.** Fixed at 14 days, or follow each campaign's configured window? Comparability across campaigns argues for fixed.
5. **Canonical timezone.** Normalising AMC to Pacific makes day-level joins with vendor data line up; keeping UTC leaves an ~8 hour offset that is harmless at week grain and wrong at day grain.
6. **FX policy.** Transaction date, period end, or a fixed budget rate for multi-marketplace rollups. This changes reported numbers, so it belongs in the contract.

**All six are now settled**, which is what moved this ADR to Accepted. Items 1 to 4 went to [ADR-017](adr-017-roas-definition.md) — fee-inclusive spend, ad-attributed revenue, halo-exclusive, a fixed 14-day window. Items 5 and 6 went to [ADR-004](adr-004-timezone-and-fx.md): source business day rather than a canonical timezone, and FX applied only in the semantic layer. The list stays here because it records what this decision could not settle on its own, and because each answer changes reported numbers rather than only code.

## Review trigger

Revisit if a second platform is onboarded and the mapping layer proves to be where the work concentrates — that would be evidence for materialising more conformed cross-channel facts (the deferred half of this decision), not for abandoning the approach.
