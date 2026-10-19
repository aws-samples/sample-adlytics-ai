# ADR-084: Display monetary values at currency scale from a server display value
Date: 2026-10-01
Status: Accepted 2026-10-01 by owner mandate ("we should use two decimals"). Implemented in the repository on `fix/pre-publication`; not yet deployed or dev accepted.
## Context
Money is stored and served at the warehouse scale, `decimal(38,4)`, so a spend tile reads
`75,459.6400 USD`. The owner reviewed the pages before publication on 2026-10-01 and asked for
two decimal places. Two shared rules constrain where the rounding can happen:
- The browser never computes analytical values ([Decision Workspace UI](../ui/README.md) §8).
- Every figure in the focus dialog is a returned server value; the browser never derives one
  ([metric-focus contract](../ui/metric-focus-contract.md) §2).

A rounded `75,459.64` is a number the server did not return, so rounding in the browser breaks
both rules. Rounding the served `value` destroys the exact value that the exact-values
disclosures exist to show, and changes what the model and calibration receive.
## Decision
**The server serves a two-place `display_value` beside every exact currency value, and every
displayed monetary figure comes from it.**
1. **One derivation.** `currency_display_value` in `src/domain/presentation/currency_display.py`
   quantizes to two places with `ROUND_HALF_UP` (ties away from zero) under a local 40-digit
   context, so a `decimal(38,4)` magnitude does not overflow the default 28-digit context. It
   refuses non-finite input, normalizes `-0.00` to `0.00`, and returns a canonical ungrouped
   string such as `"75459.64"` or `"-12.30"`. Server-authored text (metric-focus facts and the
   deterministic movers cards) uses `currency_display_text`, which groups the same rounded value
   and decides any sign on it, so a move that rounds to zero reads `0.00`.
2. **Currency only, by the value's own units.** A metric value carries `display_value` exactly
   when its `units.kind` is `currency`, and `null` otherwise. Counts, ratios, and percentages are
   unchanged. A metric-focus driver entry carries its own units and a `display_change` under the
   same rule, so Clicks under a CPC focus stays a raw count.
3. **Where it is served.** The portfolio, campaign detail (V1 and V2), and retail comparison
   value encoders, and `MetricFocus` `current`/`prior`. Chart point arrays (spend sparks, trend
   points, the detail series, the retail spark) keep the exact five-field object.
4. **Wire versions.** Artifact versions do not change: `CampaignPortfolioArtifact` 1,
   `CampaignAttributionDetail` 2, and `RetailHealthArtifact` 2. Retained preset bodies evolve
   through the ADR-070 render format instead (campaign 4 → 5, retail 3 → 4), because a page
   schema version bump would refuse every retained body until a rebake. `MetricFocus` moves
   v2 → v3: it is computed per request, so it versions cleanly with no dual decoding.
5. **The browser formats and never rounds.** It checks that a currency `display_value` is a
   canonical two-place decimal and that every other value carries `null`, then groups digits and
   appends the ISO code. It never re-derives the rounding.
6. **Exact values stay exact.** The exact-values disclosures, the retail reconciliation
   discrepancy, and stored derived values keep their own scale and their own `ROUND_HALF_EVEN`.
   The display rounding is terminal: nothing sums or re-derives a display value.
## Consequences
- Tiles, tables, the delivery evidence table, the focus head, facts, and drivers, and the
  movers cards show two places. The exact-values disclosures beside them still show four.
- **Tolerant canonical decode.** The domain decoders rebuild each artifact and refuse any
  difference from the re-encoded bytes. A retained body written before this change carries no
  `display_value`, so the rule is: a body in which no metric value carries the key decodes and
  re-encodes with it; a body in which every one does must match exactly; a mixed body or a wrong
  value is refused. The serve path re-encodes stored bodies, so every response carries
  `display_value` as soon as the API deploys. **Removal criterion:** every deployed
  environment's CURRENT pointers are at the new render formats, and one `PAGE_TTL` (1 h) has
  passed since the last API deploy in every environment.
- **Insight revisions change once.** Every insight revision hashes the measured payload, which
  now carries `display_value`, so each slot regenerates once on first serve after the deploy.
  The retail movers text is baked, so it changes at the rebake.
- **Model inputs are unchanged.** Findings still reach the model as exact figures. A narration
  citing `75459.64` for an exact `75459.6400` matches both the input and the tile; where the
  exact value has sub-cent digits the model may cite them beside a two-place tile, which is
  cosmetic.
- **The currency `daily_average` rounds twice, by decision.** The fact value is already
  quantized to four places with `ROUND_HALF_EVEN`, and the display then rounds that served value
  to two with `ROUND_HALF_UP`. Rounding twice can differ from rounding once (a mean of x.124950
  is served as x.1250 and shown as x.13). Accepted: display text is a function of the served
  value only, never of an unserved intermediate, so a reader who checks x.13 against x.1250 finds
  ordinary rounding.
- **Two places for every ISO 4217 code**, whatever its minor unit, by the owner's decision of
  2026-10-01. A zero-minor-unit currency such as JPY would show `.00`, and a three-minor-unit
  currency such as KWD would lose its third digit.
## Alternatives considered
| Alternative | Why rejected |
|---|---|
| Round in the browser through a declared formatter | Breaks the UI README §8 and metric-focus §2 rules unless both are rewritten, and puts a second copy of the rounding rule in TypeScript beside the Python one. |
| Quantize the served `value` | Destroys the exact value the disclosures show and changes what the model and calibration receive. |
| `ROUND_HALF_EVEN` for display | The display value is terminal, so the bias that justifies banker's rounding for stored derived values does not accumulate; `ROUND_HALF_UP` matches the `Intl.NumberFormat` default and what readers expect. |
| Bump the page schema version | `_v2_page_projection` would refuse every retained body at the old version, leaving every preset page unavailable until a rebake. |
| Round the unrounded mean for `daily_average` | The shown figure would disagree with the served one in exactly the tie cases. |
| Feed the model rounded inputs | Changes what derived figures are computed from. |
## Review trigger
Revisit when a currency whose ISO 4217 minor unit is not 2 enters the metric dictionary's data,
or when a page that renders `retail_product`, `marketplace_comparison`, or `data_health`
metric values ships, since those encoders do not yet carry `display_value`.
