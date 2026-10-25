# ADR-152: Read page-sized answers in SQL, with no arbitrary row ceiling
Date: 2026-10-25
Status: Proposed (2026-10-25). The owner approved the root-cause direction on 2026-10-23 (UTC):
"we need to allow the functionality to work, we cant limit arbitrarily; if a lambda would need
to be sized according to size of catalog thats guidance we should provide but we shouldnt make it
not work". This decision covers page-sized reads (R1) and the memory-model correction (R4); the
entity-wide reads of Audiences and Campaigns (R2) amend it later. Amends
[ADR-143](adr-143-size-bake-reads-by-a-bake-row-ceiling-separate-from-chat.md),
[ADR-142](adr-142-isolate-a-bake-refusal-to-its-own-scope-and-alarm-on-defects.md) and
[ADR-125](adr-125-serve-retail-product-detail-as-a-baked-child-of-retail-health.md) (markers
there).
> **Amendment, 2026-10-25 (UTC), r2a.** Decision 7 adds entity-streamed reads; Audiences is the
> first. Decision 3's "Audiences keeps it until its reads stream" no longer applies: its campaign
> section has no `rows_over_ceiling`. Campaigns' part follows in r2b.
## Context
A bake holds every row of each read until its page renders, and one memory-sized ceiling,
`BAKE_MAX_ROWS` (10,000 at 1,024 MiB), bounded every read on every page. Several pages read the
whole catalog only to show a summary:
- Retail health shows 50 products, a product count, two product sums and three movers. It read
  every product 14 times, plus every product × channel for product detail's ad section, and
  ranked, summed and picked the movers in Python. Above the ceiling the page refused, and the ad
  section silently blanked above 5,000 products on 2 channels.
- New customers listed every product per ad product to show 10 and "N more".
- Reach listed every campaign with spend only to count them.
- Scorecard, Reach and Retention read only governed axes, yet the gate counted them as catalog-wide
  and they carried a `rows_over_ceiling` refusal that couldn't fire.

## Decision
1. **A ranked restriction states the population, not just the top n.** `RankedRestriction` adds:
   rank by value or by `|current − prior|` (`ABS_CHANGE`); members with any row or a positive rank
   value (`POSITIVE`, which keeps every withheld key so the withholding survives); per-partition top n (`partition_by`, `ROW_NUMBER() OVER (PARTITION BY …)`);
   and population sums. The `ranked` CTE takes each partition's count, withholding and declared
   sums before it keeps the top n. They return as `QueryResult.population` beside the rows; the
   tool envelope is unchanged. A withheld population marks the read suppressed, so a caller
   refuses it as it refuses any withheld answer. A comparison may semi-join ranked keys.
2. **Pages read page-sized.** Retail health: one ranked statement picks the 50 and states the
   count and sums; the rows, series and product detail's ad reads are restricted to the 50; the
   movers are the top 3 by |change| (the lower key on an exact tie). New customers ranks 10 per
   ad product and counts the rest. Reach counts campaigns with spend as a one-row ranked read's
   population.
3. **A bounded read cut short is a defect, never a refusal or a blank.** Retail's reads, the
   grouped answers of Scorecard, Reach, Retention and New customers, and the bake spend roster no
   longer raise `rows_over_ceiling`; Audiences keeps it until its reads stream (R2).
4. **The ceiling is derived from memory.** Governed axes (days, marketplaces, media-taxonomy
   terms) count at their own size. `BAKE_MAX_ROWS = bake_max_rows(BAKER_MEMORY_BYTES)`, from the
   binding row's intercept and slope (`domain/operations/bake_memory.py`); `make
   check-bakeability` recomputes both and prints the ceiling per Baker size.
5. **Every limit that remains names its operator action.** `OPERATOR_ACTIONS` maps each `limit`
   refusal to the action the `isolated_refusal` line states; for `rows_over_ceiling` and
   `population_over_ceiling` it's "raise the Baker memory", with the memory the model assumed.
6. **Chat's `measurement_top_n` is not decided here.** It still ranks in Python under chat's
   500 / 10,000 narration cap, which stays.

7. **An entity-wide page renders one entity at a time.** Each of its reads streams `ORDER BY` its
   grain through a `GroupSink`, a fold on the existing port that hands one entity's figures at a
   time across a one-slot queue; the render merges the reads by key. Memory is the groups in
   flight, never the population. Audiences reads every DSP campaign this way (ADR-141): a count
   pass in the render builds the index, and a page pass, answered by result reuse, renders each
   campaign's pages as the stage writes them. These reads have no row `LIMIT`
   (`RowCeiling.BAKE_GROUPED`, folded only): rows cost time, not memory. What stays held (the
   index, its segment set and one campaign's segments) is bounded by the one derived
   `BAKE_MAX_ROWS`; Audiences' history walk keeps only the campaigns it grades.
## Consequences
- Retail health, New customers and Reach bake at any catalog size; Retail runs 49 plans per
  preset. The probe measured Retail's render peak at about 1.57 MB at both 10,000 and 100,000
  products (P1, 2026-10-25, local).
- On an exact |change| tie for the third mover, the lower ASIN is named, not the lower title
  (render format 11).
- A product with no `dim_product` identity outside the shown 50 and the movers no longer stops
  the Retail bake.
- A row outside the governed taxonomy now fails as a defect where it used to refuse the page.
- The composed statements match the Python rule on Athena at 100,000 generated keys: Retail's
  top 50 with count and sums, the movers, and the gateway with and without withholding (P2
  `ranked` of `validation/probe_bake_row_ceiling.py`, dev, 2026-10-25) [verified]. A unit test
  runs the gateway statement on SQLite to hold the filter order that keeps withholding.
