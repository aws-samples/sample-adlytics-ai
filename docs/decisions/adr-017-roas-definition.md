# ADR-017: The default ROAS is ad-attributed, halo-exclusive, and fixed at 14 days

Date: 2026-08-19
Status: Accepted

## Context

[ADR-002](adr-002-canonical-data-model.md) settles the *structure* of the canonical model and explicitly cannot settle what the metrics mean, because those are business calls. [Q-13](../decision-coverage-registry.md#question-register-q-nn) lists six. Items 5 and 6 — timezone and FX — were settled by [ADR-004](adr-004-timezone-and-fx.md). **This decision settles the remaining four**, which closes Q-13.

**Why four choices in one ADR rather than four.** They are not independent. Each is a clause in the sentence "ROAS means X", and settling any one while the others float leaves the metric undefined — a halo decision is meaningless without a numerator decision, a window only applies to an attributed numerator, and a numerator without a denominator is not a ratio. This is one decision with four dimensions, so it is one file.

### There are two ROAS metrics because the numerator can come from either measurement system

Both use the same denominator. **Vendor Central contains no advertising spend and no campaign dimension** — the sales report carries `shippedRevenue`, `shippedCogs` (cost of goods, i.e. the wholesale price Amazon paid, not ad cost), `shippedUnits`, `customerReturns`, and option-dependent `orderedRevenue`/`orderedUnits`, and nothing else [verified, [sources/vendor-central](../sources/vendor-central.md) §3]. Ad spend only ever comes from AMC. So the denominator is AMC ad spend in both cases and only the numerator differs:

| | Numerator | Campaign grain | Basis |
|---|---|---|---|
| attributed | AMC attributed revenue | yes | `ad_attributed` only |
| blended | Vendor Central shipped revenue | **no** | mixes two |

The blended figure is the inverse of TACoS and answers a real question — whether advertising is growing total demand or buying credit for demand that already existed. It cannot be decomposed by campaign because nothing in retail revenue records what caused a sale.

### Halo is ad-exposed, not organic

`product_sales` excludes brand halo, `total_product_sales` includes it, and AMC documents the identity `total_product_sales = product_sales + brand_halo_product_sales` [verified, [sources/amc](../sources/amc.md) §3]. Halo is a purchase of a *different, non-advertised* product from the same brand by someone who **did** see the ad. It is not organic demand. True organic — a purchase with no ad exposure at all — requires `conversions_all` and its `exposure_type` column, which is a paid feature free only through 2026-12-31 ([Q-07](../decision-coverage-registry.md#question-register-q-nn)).

`tracked_asin` includes same-brand products that were not advertised, and the source doc records that conflating advertised with halo performance moves ROAS in the direction that flatters it [verified].

### The window is chosen by picking a table, not by setting a parameter

The attribution window itself is campaign-determined and cannot be changed in AMC [verified]. What we choose is which attributed-events table to read:

| Table | Event selection | Stability |
|---|---|---|
| `amazon_attributed_events_by_conversion_time` | conversions in window, traffic up to **14 days before** | stable |
| `amazon_attributed_events_by_traffic_time` | traffic in window, conversions up to **30 days after** | **conversion range auto-extends 30 days past the window end, and the docs state output may change over time** |

Both are [verified]. The `_by_traffic_time` property is decisive: the same query over the same past period returns different numbers on different days.

## Decision

**One. The default ROAS is `roas_attributed_14d`** — an ad-attributed numerator over ad spend. `roas_blended` remains a first-class metric, offered by name at the grains it supports, carrying `mixes_basis: true`. It is never the default and never available at campaign grain.

**Two. The default excludes brand halo, and halo is exposed as its own metrics** rather than folded into the default numerator. Three metrics are declared: revenue excluding halo, revenue including halo, and the halo portion alone. A halo-inclusive ROAS is available by name.

**Three. The window is fixed at 14 days**, taken from `amazon_attributed_events_by_conversion_time`, and every metric derived from it carries the window in its name. There is no configurable window and no per-campaign variant.

**Four. The denominator is fee-inclusive spend** — `ad_spend`, from `total_cost` on DSP and `spend` on sponsored ads. Not `spend_media_only`, for a reason that is decisive rather than a preference: `impression_cost` has **no sponsored-ads equivalent** [verified, [sources/amc](../sources/amc.md) §6], so a media-only default cannot be computed for one of the two ad channels. A default that does not exist everywhere is not a default. Fee-inclusive is also what the customer actually pays, and it is the larger denominator, so it produces the more conservative ROAS. `spend_media_only` stays modelled and available by name for DSP fee decomposition.

**Consequential rename.** `roas_total` becomes `roas_blended`. "Total" read as "the complete one", which invited exactly the misreading that it was the better figure.

### The ordering these three follow from

Stated because it is the reusable part, and because all three resolve the same way once it is written down. Every one of these choices had an option that biases the headline number upward.

1. **Reconcilable beats impressive.** Default to the figure a customer can verify against their own Amazon reporting.
2. **Stable beats complete.** A number that changes on refresh destroys more trust than a number that is conservative.
3. **Named beats implicit.** The window is in the metric name, the basis is on every row, halo is its own measure.
4. **Separate beats blended.** Halo, organic and total are distinct answers, not ingredients in one headline.

## Consequences

**The default understates true impact, deliberately.** Attributed revenue counts only what AMC's attribution catches inside 14 days. Sales our ads genuinely influenced but that fell outside the window are absent from the headline figure. This is a knowing trade: the alternative is a number nobody can check.

**The blended figure must be asked for.** A customer who wants total-business efficiency has to name it, and will be told it spans two measurement systems that never reconcile. `mixes_basis: true` makes the narration layer surface that; it is not optional.

**Halo becomes more visible, not less.** Because the halo portion is its own metric, "how much lift do my ads generate on products I am not advertising" is a direct question with a direct answer instead of an invisible component of every ROAS. This is the intended effect: excluding halo from the default is not hiding it.

**A stated window creates a support burden we accept.** Customers comparing our figure against a surface using a different window will see a difference. The window in the metric name is what makes that conversation short.

**`_by_traffic_time` is now effectively excluded from the model.** Any future contract wanting it reopens this decision, because adopting it reintroduces output drift.

**Fee decomposition needs care.** With fee-inclusive spend as the default, `dsp_impressions` mixes two scales in one table — `total_cost` and `impression_cost` are millicents while every fee column and `bid_price` are microcents. A divisor keyed on the table makes every fee figure wrong by exactly 1000×, in the direction that makes fees look negligible, and fee-inclusive ROAS still reconciles against `total_cost` so the headline looks correct while the decomposition is garbage. The per-column divisor rule already exists to prevent this; making fee-inclusive the default raises its importance.

**Q-13 is closed by this decision.** All six metric definitions are now settled — timezone and FX by [ADR-004](adr-004-timezone-and-fx.md), and revenue, halo, window and spend here.

## Alternatives rejected

**Halo included by default.** The strongest case for it: halo is real ad-driven demand, and excluding it understates advertising's value on a metric customers use to justify budget. Rejected because it biases the headline upward and is hard to explain. A tool reporting better numbers than Amazon's own console is one cross-check away from losing credibility, and the first thing a customer does with a new number is compare it. Which Amazon surface defaults to which treatment needs confirming against a live account [unverified], but the asymmetry holds regardless: reporting lower and explaining why is survivable, reporting higher is not.

**Blended ROAS as the default.** Defensible on the grounds that it is the truer business figure — real money over real money, no attribution modelling. Rejected because it cannot be decomposed by campaign, which is where decisions are made, and because it mixes measurement bases, so the product's headline metric would permanently carry a caveat.

**A configurable attribution window.** Rejected twice over. A per-campaign window needs a table with output drift, and offering the choice turns every conversation about a number into a conversation about settings. Comparability across campaigns also disappears the moment the window varies by campaign.

**Media-only spend as the denominator.** The case for it: media cost is the part a media team controls, so ROAS on it measures buying skill rather than fee structure. Rejected because `impression_cost` exists only for DSP. A default that silently means one thing on DSP and something else on sponsored ads is worse than either option consistently applied.

**Storing ROAS.** Never considered viable and recorded for completeness: ratios are not summable, so every ratio is derived in the semantic layer from additive facts. This is already a contract rule.

## Review trigger

Reopen if any of these becomes true:

- The paid-feature decision ([Q-07](../decision-coverage-registry.md#question-register-q-nn)) lands `conversions_all`, making true organic separable. That does not change the ROAS default but adds a genuinely new metric and changes what halo should be compared against.
- `fct_incremental_effect` is built (phase 5), making `roas_incremental` computable. Incrementality attributes causally rather than by attribution rule, and is the first candidate to displace the default.
- A live-account check shows Amazon's own surfaces default to halo-inclusive. The reconcilability argument then points the other way.
