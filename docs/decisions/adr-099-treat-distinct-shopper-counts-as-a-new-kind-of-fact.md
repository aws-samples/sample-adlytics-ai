# ADR-099: Treat distinct shopper counts as a new kind of fact

Date: 2026-10-06
Status: Proposed. The owner approved the business-outcomes design on 2026-10-06. Partly implemented
(2026-10-08): the `amazon_ads` media profile and the point 6 contract gates exist; the pack fields of
points 7 and 10 exist ([ADR-110](adr-110-declare-query-pack-parameters-period-policy-and-cadence-lag.md));
`src/domain/shopper_aggregates/` holds the point 8 to 10 rules (closed enumerations with
`not_returned`, loss grading, the bar and repeated-total checks, disjoint sums); the null meaning
`not_returned`, the point 12 residual and control-total exclusions, and the widened ADR-087 rule are
in. The first shopper-aggregate pack and fact, `amc-new-to-brand-buyers-platform` and
`fct_new_to_brand_buyers` (2026-10-09), wire the load: a `discard_suppressed_rows` delivery skips the
non-additive refusal, passes the bar check, drops its withheld rows, writes its one expected platform
cell `not_returned` when nothing survives, and records the cell loss on its publication evidence. The
bake wiring (rosters) arrives with the first page that reads it. Not deployed.
Amends, once accepted: [ADR-019](adr-019-grain-viability-is-measured.md) (scoped exception),
[ADR-034](adr-034-a-withheld-residual-is-keyed-on-its-delivery.md),
[ADR-035](adr-035-suppression-is-a-quantity-not-a-flag.md), [canonical model §4a](../canonical-model.md),
and the typed-absent scope of ADR-087 on `feat/insight-data-ux-refresh`. The current design is in
[Business outcome measurement](../concepts/business-outcome-measurement.md).

## Context

Four of the seven business outcomes ask how many **different shoppers** did something in a period:
how many saw DSP, saw both DSP and sponsored ads, bought for the first time, or bought again. AMC
counts them inside the clean room with `COUNT(DISTINCT user_id)`. The shopper ID never leaves AMC
(`docs/sources/amc.md:129`).

A distinct count is a new kind of fact for this repository, and it breaks the two premises
[ADR-019](adr-019-grain-viability-is-measured.md) rests on.

- **It can't be summed.** Monday's shoppers plus Tuesday's isn't two days of shoppers, because some
  shopped both days. AMC has no approximate distinct count or mergeable sketch to export
  (`docs/sources/amc.md:393`). So a coarser grain can't be derived from a finer one. Every grain we
  serve needs its own extraction. ADR-019 decides the opposite: one extraction at the finest grain,
  coarser grains derived.
- **Withheld rows can't be placed.** Our contracts map only `NONE` and `LOW` columns, so a withheld
  row loses every dimension ([ADR-034](adr-034-a-withheld-residual-is-keyed-on-its-delivery.md)
  Context). AMC may merge such rows or, when a distinct count is selected, keep duplicates
  (`contracts/amc/amc-reach-frequency.yaml:201-206`, live evidence of 2026-08-27). Either way the
  row can't be matched to the cell it came from. So ADR-019's viability measure, suppressed rows over
  total rows, can't say which cells were lost. A campaign delivery missing 40 of 100 campaigns can
  read as 1 withheld row in 61.

Today the reach pack refuses the whole delivery if any row is withheld
(`contracts/amc/amc-reach-frequency.yaml:201-206`; `src/domain/curation/assertions.py:245-254`). Applied to
every new pack, one small campaign or ad product would blank a whole table. ADR-034 says a withheld row
can be neither stored nor discarded, because its measures belong in a total; ADR-035 folds withheld
quantities beside the attributable figure. Neither fits a quantity that no total is built from.

## Decision

**A distinct shopper count is an explicit periodic fact with a closed set of served grains. Each served
grain is its own extraction. Withheld rows are discarded, surviving rows are kept exactly, and every
expected cell that didn't come back reads "not available".**

1. **Shape.** Every shopper-aggregate fact is an explicit periodic fact
   ([ADR-046](adr-046-separate-extraction-coverage-from-measurement-time-grain.md) class 2). It
   carries the exact period, its `time_grain`, a `grain_set` literal, a `cell_state`
   (`returned` or `not_returned`) and an `identity_unit`. Distinct counts are typed `exact_distinct`.
2. **Closed grain set, one contract per grain set.** A fact declares the grain sets it serves (for
   example platform, ad product, campaign). Each is one contract and one extraction
   ([ADR-022](adr-022-contract-decomposition-by-dataset.md)). Media columns a grain set excludes are
   NULL with meaning `not_applicable_under_options`, through a grain-set media profile such as
   `amazon_ads`. There's no `__all__` sentinel. `grain_set` is an exact selector.
3. **Distinct-count rules.** An `exact_distinct` metric is read only at its extracted grain set and
   period; a coarser request is refused with the nearest served grain as the alternative. It's never
   summed across time, channel, campaign, segment or follow-up month. Missing is never zero.
4. **No figure combines deliveries of different contracts or grain sets for the same period.** Three
   exceptions are declared: (a) the same metric, contract, grain set and bucket scheme across periods
   or vintages, shown as a labelled change; (b) the declared mixed-basis cohort ratios of
   [ADR-104](adr-104-define-acquisition-cohorts-by-amazons-new-to-brand-flag.md); (c) a daily-additive
   numerator over a periodic denominator, aligned by
   [ADR-100](adr-100-serve-periodic-metrics-only-for-complete-measured-periods.md). Any other
   cross-delivery sum, difference or ratio is refused. This is a population rule, not a privacy
   control.
5. **Additive measures on these facts** (impressions, window spend, purchases, revenue) are `sum`
   and periodic. They're summable only across the disjoint axes of
   [ADR-101](adr-101-model-shopper-groupings-as-disjoint-axes-with-one-window-per-shopper.md), inside
   one fully returned delivery, and `exact_only` on every other axis.
6. **Packs emit source labels only.** The canonical owner resolves channel and grouping keys through the
   one taxonomy ([ADR-052](adr-052-separate-ecosystem-platform-channel-and-ad-product.md),
   [ADR-044](adr-044-materialize-source-aligned-data-and-centralize-canonical-publication.md)). No
   contract sums an additive measure from the attributed-events tables above campaign grain. Each row
   pairs one conversion with the traffic event it's credited to
   (`contracts/shared/amc-columns.yaml:1982`), and neither table's column list has a conversion ID
   (`amc-columns.yaml:1995-2815`, `:2836-3656`). So one purchase may be credited to several campaigns, and
   nothing in those tables can check that it counts once [inference]. The rule is safe whether or not
   that holds. Above campaign grain, purchases come from `conversions`, one row per conversion
   (`amc-columns.yaml:1452`). Existing deviation, information for the owner: three shipped packs derive
   `channel_group_key` with a SQL `CASE` (`contracts/amc/query-packs/amc-attributed-conversion.sql:1`,
   `amc-deduplicated-temporal-attribution.sql:8`, `amc-reach-frequency.sql:1`). The gate names exactly those
   three as version-1 exceptions (`EXISTING_DELIVERED_TAXONOMY_KEYS`,
   `src/domain/data_contracts/validation.py:96`); a new version of any of them loses the exception. Fixing
   them is outside this design.
7. **Suppression policy.** A pack declares `suppressionPolicy`. Every new shopper-aggregate pack uses
   `discard_suppressed_rows`: withheld rows are dropped before the fact write, their additive measures
   go to the `_withheld` residual, their distinct counts are dropped, and surviving rows publish.
   `reject_delivery` stays for sponsored reach and the new DSP reach pack. The choice between the two
   rests only on probe P-BO-1's exactness gate: surviving counts must equal the controls-off values.
8. **Expected cells.** A pack declares a closed enumeration, checked at load, or a roster, checked at
   bake. A missing enumeration cell becomes a `not_returned` row with null measures. `not_returned` is
   a new null meaning: it claims neither "suppressed" nor "empty", because the two look the same. A
   missing roster cell renders "not available". An unexpected enumeration value rejects the delivery.
9. **Loss is missing expected cells over expected cells.** It's computed per identity-space slice for
   the exposure and frequency packs, and per acquisition-source slice for cohorts, where a cohort-group
   with no row at any follow-up month is an absent cohort outside the denominator. ADR-019's 5% degrade
   threshold grades a slice. Its 20% withdraw threshold applies only where the expected-cell set has
   **20 or more cells, counted per delivery, and a coarser served grain set of the same fact exists**.
   Even then the cells still read "not available" with a pointer to the coarser view. Every other set
   is graded, never withdrawn.
10. **Bar check.** Each pack declares `expectedBar`, default 2. A delivery is rejected when an
    unsuppressed row reports `distinct_user_count` above it, because that means a higher-threshold
    column slipped in. If a derived label escalates the bar to 100, that pack ships a new version with
    `expectedBar: 100` at narrower grains.
11. **The differencing-guard risk is accepted.** Re-extraction is required, so the schedule is the
    pattern ADR-019 warned about. It's bounded by a shed order, measured at onboarding by one month of
    the real schedule (probe P-BO-1 Step B), and the first lever is to emit several grain sets from one
    execution with `UNION ALL`.
12. **Totals.** The control-total check skips distinct-count measures, and the `_withheld` residual
    excludes them.

ADR-019 stays unchanged for additive measures in existing packs. The exception covers `exact_distinct`
measures and packs that declare `discard_suppressed_rows`.

## Consequences

- Every period and grain shown costs an AMC execution: about 26 to 31 per marketplace per month for the
  planned schedule, of which 4 need paid enrolment. That's an arithmetic projection from the pack list,
  and it excludes D1a (DSP reach per campaign-day), which runs on the existing DSP impressions cadence.
  Whether AMC accepts it is unverified until Step B runs.
- A page can show a table with some cells reading "not available" instead of a blank table. A degraded
  slice carries a server-built note.
- The freshness registry records loss per slice and per delivery. The discarded-row count stays a
  diagnostic only.
- The null-meaning enum gains `not_returned`. The forecasting design's wording "the four null meanings"
  becomes "the repo's null meanings" in whichever branch lands second.
- ADR-087's typed-absent rule (ratios only) widens: an `exact_distinct` value may be `null`. Both
  branches have met, so ADR-087 carries the marker; the rule's one home is
  `Metric.absence_is_typed` (`src/domain/catalog/capability.py`).
- Probe P-BO-1 gates every distinct-count pack. If its exactness gate fails, the gated packs fall back to
  `reject_delivery` at the grains least likely to suppress.

## Alternatives considered

- **Derive coarser grains from one finest extraction (ADR-019).** Wrong for a distinct count: it can't be
  summed, and AMC has no sketch to merge.
- **Reject the whole delivery on any withheld row.** Safe, but one small campaign blanks a page.
- **Keep withheld rows in the fact.** They carry no cell identity, so they can't be shown or summed.
- **One table per grain.** Encodes grain in a table name, which the data-contract steering forbids.
- **An `__all__` sentinel for excluded media columns.** A sentinel reads like a real value; NULL with a
  declared meaning doesn't.
- **Withdraw any breakdown at 20% loss.** With four or five ad products, one missing product is 20–25%
  loss and would pull the whole channel table. Rejected under the owner's preference for an explicit
  "not available" over hiding (2026-10-06).

## Review trigger

Reopen when P-BO-1's exactness gate fails, when Step B records a differencing rejection of a monthly
platform-grain pack, or when AMC documents an approximate distinct count or mergeable sketch.
