# Dev seeding

| | |
|---|---|
| **Type** | Environment concept — the fictional development data estate |
| **Status** | Local fixture and bulk-scale implementation complete 2026-09-18; the declared shape and the 2025-07-01 span (ADR-086) generated locally 2026-10-05, dev landing pending. The earlier generation was landed and probed in development ([validation results](../../validation/RESULTS.md), 2026-09-22 and 2026-09-24). Observation of each §4 designed state on a served page is not yet recorded. |
| **Owns** | What the dev lake must contain, how it is generated and landed, and what a seeded environment must prove |
| **Parent** | [Architecture](../architecture.md) · fixture provenance: [ADR-051](../decisions/adr-051-classify-fixture-provenance-and-evidentiary-status.md) |

Dev seeding exists so the deployed development environment behaves like a customer's
environment: every product surface renders from substantial data, every designed state is
reachable on real served pages, and a claim that "the system works" has a population large
enough to falsify it. This document owns the seeding strategy.

## 1. Generate deliveries, not facts — simulate the customer's load cadence

Customers do not hand the platform pre-aggregated fact rows; sources deliver on their own
schedules and the pipeline builds the lake. The generator therefore emits **simulated source
deliveries** that land through the real ingestion path under new operation ids, never
pre-shaped canonical rows:

- **AMC**: a customer authors a query with an attribution lookback and schedules it daily.
  Each simulated daily run returns rows for **every campaign active in its window** and
  re-emits prior days across that contract's declared `restatement_window_days` (capped only
  by available fixture history) as late attribution lands — so restatement, re-bake, and
  publication-pointer advance are exercised by construction, not asserted by hand.

  > **Amendment, 2026-09-22 — bounded bucket span per landed delivery.** A simulated run's
  > trailing window no longer lands as one delivery. The generator splits each run into an
  > ordered oldest-first series of chunked deliveries, each within the per-derived-table
  > bucket-span bound owned by
  > [Load Path write-discipline §2.1](../capabilities/load-path/write-discipline.md)
  > (2026-09-22 amendment, which also owns the bound's value and argument) and each carrying
  > its own operation identity, manifest, and immutable objects. This document owns only that
  > the generator is the dev landing seam that applies it. Restatement is unchanged: later
  > runs still re-emit prior days, chunked the same way.
- **Vendor Central**: one source-valid `DAY` report per date (both aggregate and by-ASIN
  arrays, each required `endDate`, and every complete nested `Amount`), plus a later immutable
  sales report for the same date whose `lastUpdatedDate`, returns, revenue, and COGS reflect
  the returns lag. Vendor revenue is net of returns;
  [sources/vendor-central](../sources/vendor-central.md) owns the semantics).

Cadence simulation and value shapes are orthogonal and both deterministic: the cadence
machinery decides *when rows arrive and re-arrive*; the declared scenario shapes (§2) decide
*what the values are*. Nothing is random: every value is a seeded SHA-256 draw over declared
constants, and the generator reads no clock
([ADR-086](../decisions/adr-086-shape-dev-seed-data-with-declared-deterministic-variation.md)).

The span runs from 2025-07-01 to a declared `last_day`, the run date of each landing commit. A
restating Vendor report is landed only once it has fallen due, so the last two days carry only
the first report. Where the dev lake still holds partitions of a retired generation or of
retired traps, the landing planner outranks them with a dev-only watermark offset (ADR-086 §15). Dev
carries no AMC attribution maturation step (`attribution_maturity` is `{1, 1}`), so late
attribution re-arrives through the restatement window with unchanged values.

**How a landing is phased.** Each delivery has a class (`cadence`, `whole`, `trap`, `seal`,
`backfill`), and `land_fixtures` selects by `--classes` and, for `cadence`, `whole` and
`backfill`, by `--run-days-from` and `--run-days-to` (ADR-086 §14 owns the reasons):

1. **Phase 0**: `--classes trap,whole,seal`. The traps, the product hierarchy (in row chunks
   within the Stage-1 attempt cap, ADR-086 §18) and the other whole deliveries, and the
   campaign-dimension seal. It needs a `--settled-report` from
   `validation/probe_pipeline_settled.py` no older than 15 minutes, and it lands the seal one
   delivery at a time, each awaited until CURRENT (5 s polls; 2,880 s timeout, which outlasts the
   semantic-write queue's redeliveries, ADR-086 §14).
2. **Phase A**: `--classes cadence --run-days-from <last_day − 55>`.
3. **Phase B**: `--classes backfill --run-days-to <last_day − 56>`, in batches by run day, each
   older day once at its final values.

A **daily increment** selects `--classes cadence,whole --run-days-from <run day>`; it never
selects a trap or a seal, and every later daily run folds the campaign dimension from a sealed
or later partition. **If a seal delivery times out**, the landing stops with no further put.
Diagnose the cause first, then raise `shape.seal_generation` by one in the catalog and re-land
the whole seal with `--classes seal` and a fresh settled report; the new generation lands at
fresh prefixes. The seal is this seam's application of
[write-discipline §2.1](../capabilities/load-path/write-discipline.md) rule 3.

**Reloading one corrected feed at an unchanged span** (added 2026-10-22). A delivery's keys
follow from its declaration's `retrieved_at`, which follows from `last_day`, so a corpus corrected
without a new `last_day` plans to keys dev already holds with the old bytes, and the immutable put
refuses them. The deduplicated-attribution feed therefore carries its own retrieval generation,
`shape.deduplicated_retrieval_generation` (0 to 3) in the catalog. Generation `g` puts each run's
chunk `i` at second `g × 16 + i` of the run's 64-second stride: the same rows, windows and
watermarks at fresh keys, on the same run slot, so the replay band classifies each delivery as
before. Generation 1 carries the corrected corpus (§2, every model on every day); generation 0's
keys keep the rotating rows. The reload selects that feed alone with `--classes cadence
--fixtures generated/amc-deduplicated-temporal-attribution.csv --run-days-from <last_day − 55>`
under a new operation id. Each day's final run ties the watermark already CURRENT, and ties
publish, so the newer delivery replaces it. Earlier runs carry an older watermark and are skipped,
except on the retired-floor days (ADR-086 §15), where every run ties. This feed's rows don't mature
between runs, so either way each day ends on the corrected rows. The first selected run re-emits
its trailing restatement window, so the reload covers every day from `last_day − 55 −
(restatement_window_days − 1)` (2026-07-14 at the 2026-10-06 span and the contract's 30 days).
Only days before that keep generation 0's rows, because a phase-B delivery is refused
(`FULL_HISTORY_NOT_ENABLED`); dev holds no dedup day before 2026-07-14 (read 2026-10-22).

## 2. Declared per-entity scenario shapes

Every seeded entity carries a **declared scenario shape** in the governed synthetic entity
catalog (`contracts/shared/synthetic-entity-catalog.yaml`) — deterministic, reviewable, named:
growth ramp, stable growth, sustained decline, spike-and-fade, returns-pressure product,
flat-traffic-rising-conversion, zero-conversion campaign. Near-linear generated values are a
recorded defect: they degenerate insight slots and leave state paths untested.

The catalog's `shape` section declares the rest (ADR-086 owns the reasons):
- a weekly pattern and a monthly seasonality per entity, a long-run trend, and a 56-day story
  ending on `shape_anchor`;
- per-campaign constants (level, growth, slope, noise, CPC, CTR, conversion rate, order value,
  viewable share, frequency), so no two campaigns move identically;
- purchases from clicks through the conversion rate, revenue from purchases through the order
  value; daily conversion stays at or below 30 %, and every 7-day window keeps purchases ÷ clicks
  in 4–25 % and attributed ROAS in 1.00–9.00;
- Vendor revenue net of returns, returns lagging shipments by 3, 7, 14 and 21 days, and served
  revenue per unit shipped inside 12.00–42.50;
- a price shift on one product, which moves its revenue per unit while units hold.

Shape invariants, each falsifiable against the lake. The three the 2026-10-04 UX review found
broken on the earlier generation (returns above shipped units, an implausibly small day, one CTR
on every campaign) are also pinned over the committed fixtures by
`tests/unit/test_display_data_invariants.py`, because the page shows no data-quality notes
(ADR-086 §17); so is the deduplicated-attribution invariant, the last below:

- **A campaign active in a window has a row for every day of that window.** A zero-delivery
  day (before the launch, or a pause day) is a present row with every measure 0, never an
  absent row — absence is reserved for the declared coverage gap. On a zero-delivery day the
  attributed-conversion contract has no row, as a grouped conversion query emits none.
- **Conversions pair with exposure identities.** Attributed-conversion rows reference only
  campaign identities that exist in exposure and the campaign dimension.
- **Ratios stay inside their real-world ranges**: clicks ≤ impressions, viewable ≤ measurable ≤
  impressions, distinct users ≤ reach ≤ impressions, returned units below shipped units on every
  day and in every 7-day and 14-day window, COGS below revenue except where a declared
  margin-pressure scenario says otherwise.
- **Product names are valid from the hierarchy's first dev observation** (2026-08-28, read
  2026-10-05), because the hierarchy is a system-first-observed snapshot (ADR-086 §16).
- **Deduplicated attribution credits every model on every day** (added 2026-10-22, after the
  Scorecard showed DSP and Sponsored Brands at 0.00× because the feed held one row per day,
  rotating its one model and one channel). The governed query credits each matched purchase
  once under each model from one ranked set
  (`contracts/amc/query-packs/amc-deduplicated-temporal-attribution.sql`), so:
  every covered day carries every model registered in `contracts/shared/attribution-models.yaml`;
  per day, `matched_purchase_credit` and revenue are equal across models; every channel with
  attributed purchases that day has linear credit above 0; and the day's deduplicated revenue
  never exceeds the sum of the channels' halo-inclusive attributed revenue. Two consistency
  clauses keep the feed one the query could produce (added 2026-10-22, review): no model credits
  a channel more than its own attributed purchases or halo-inclusive sales that day, since it
  credits only purchases the channel touched; and on a day where models credit a channel
  different revenue, the deduplicated revenue sits below the attributed sum, since only a
  purchase several channels touched moves between models and the attributed sum counts it once
  per channel. The generator reads each channel's measures from the attributed-conversion
  corpus and declares, in `shape.amc.deduplicated_attribution` of the synthetic catalog, the
  share of each measure that comes from purchases several channels touched and how many
  channels each touched. The day total counts those purchases once, so it is below the
  attributed sum and the same under every model. Every model credits a channel its unshared
  measure and a part of the shared purchases weighted by the channel's shared measure and the
  model's declared channel weight. Linear's equal weights split each shared purchase evenly
  across its channels, so its credit sits below each channel's own measure; first and last touch
  credit whole purchases. A campaign on its declared coverage-gap day contributes nothing, as on
  every other contract.

## 3. Two population tiers

- **Visible tier, ~60 campaigns, one marketplace.** 50 fills the portfolio row bound; the
  remainder makes pagination past the bound and the boundless population total real on served
  pages ([ADR-071](../decisions/adr-071-reach-the-full-campaign-estate-through-baked-ordered-segments.md)),
  and pushes the bake envelope into ADR-070's named per-campaign decomposition. Do not exceed
  ~60 without that decomposition in place — the envelope arithmetic is owned by the
  [campaign operational design](../ui/campaign-portfolio/operational-design.md) §1.
- **Bulk tier, exactly 1,000 campaigns, one fact delivery.** Identities expand only on the
  explicit scale/test path from the governed zero-padded canonical, source, and display-name
  patterns in the synthetic catalog. The normal visible generator and landing bundle remain
  the same 60-campaign source scope; bulk never enters default dev landing. The bulk scenario
  assignment cycles deterministically through all six declared campaign scenarios. This tier
  falsifies "works only with small data": local acceptance asserts complete baked orderings,
  segment integrity, roster reach, pagination totals, individual-body byte bounds, and records
  measured render duration under ignored validation evidence.

## 4. In-window designed states, seeded deliberately

A designed state that no dev data reaches is untested UI. Each state is declared as an offset
from `event_anchor` (or, for the launch, `shape_anchor`), so it falls inside the 1D, 7D and 14D
windows ending on the run date:
- a promotion spike on two campaigns (−5 to −3), a budget shift between two campaigns (−4,
  held after), a pause (−2 to −1) and a launch (−10 from the shape anchor), which together give
  the zero-denominator states: a zero prior with a non-zero current, both zero, a zero current
  with a non-zero prior, and a launch campaign whose 14D prior has no delivery;
- a coverage gap on one campaign-day (−4), with no row on any contract that day;
- ordered units diverging from shipped units on one product (from −9, held after), a
  return-rate rise on one product (from −10), and a price cut on one product (from −8);
- a restatement (proving re-bake); an unnamed campaign; a campaign whose delivery evidence is
  genuinely incomplete (so the
  [ADR-072](../decisions/adr-072-name-why-evidence-is-insufficient-with-governed-slot-conditions.md)
  evidence status sheet renders its missing-days reading on live data); and a campaign with no
  covered conversions (the attribution `empty_covered` reading).

Suppressed and withheld rows are carried by the hand-built traps, not the generated cadence
(ADR-086 §13). A vendor reconciliation mismatch is not seeded: it would quarantine the whole
day's Vendor delivery (ADR-086 §4).

## 5. Retired fixture defects (verified 2026-09-17; generator corrected 2026-09-18)

These observations describe the superseded generated deliveries and remain as regression
history. The current local generator enforces §2. On 2026-09-22 the deployed replacement passed
the AMC current-invariants lake probe with zero click-above-impression violations and zero
unmatched conversion pairs, recorded in `validation/RESULTS.md`:

- **Round-robined exposure rows**: exactly two campaigns receive an exposure row per day, so
  no campaign has complete daily coverage in any window — the sole cause of every dev
  campaign detail refusing delivery as insufficient. §2's every-day invariant retires this.
- **Unpaired conversion keys**: attributed-conversion fixtures reference sponsored campaign
  keys that exist nowhere in exposure or the campaign dimension, and carry no rows for the
  DSP campaigns — the cause of the attribution `empty_covered` details. §2's pairing
  invariant retires this.
- **Out-of-range ratios**: clicks exceed impressions on every row, returned units exceed
  shipped units, and COGS exceeds revenue — honest arithmetic on degenerate inputs. §2's
  range invariant retires this.

## 6. Provenance and acceptance

Every seeded artifact carries its [ADR-051](../decisions/adr-051-classify-fixture-provenance-and-evidentiary-status.md)
provenance class; all values are fictional, and the catalog's reserved prefixes and
no-live-identifier rules apply unchanged. A new delivery is immutable: the pinned
`retrieved_at` revision advances, and landing plus reprocessing run under a new operation id.

An environment counts as **seeded** only when: the three pages render populated from the
visible tier with presets resolvable; each §4 designed state is observed on a served page;
the §2 invariants hold against the lake by query; and the bulk tier's assertions pass with
the bake duration recorded against the envelope.
