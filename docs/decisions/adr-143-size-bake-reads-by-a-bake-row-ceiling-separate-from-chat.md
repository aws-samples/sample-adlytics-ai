# ADR-143: Size bake reads by a bake row ceiling separate from chat's
Date: 2026-10-18
Status: Proposed (2026-10-18). The owner approved a separate bake ceiling on 2026-10-18 (UTC),
proposed at 50,000 rows, with the final value set by a dev probe. Chat is unchanged. Later the
same day the owner also approved ranking Retail health's top 50 inside the query (decision 7). The
probe ran on dev on 2026-10-18 and set `BAKE_MAX_ROWS` to 10,000 (decision 8).

> **Proposed amendment, 2026-10-25.** [ADR-152](adr-152-read-page-sized-answers-in-sql-with-no-arbitrary-row-ceiling.md) (Proposed) would amend decisions 6 and 8: `BAKE_MAX_ROWS` is derived from the Baker's memory (`bake_max_rows`, 10,835 at 1,024 MiB when this was written on 2026-10-25; `make check-bakeability` prints today's value, 22,544 on 2026-10-26) instead of a probed constant, governed-taxonomy axes count at their own size, and only entity-wide reads still hold a population. Decision 7's ranked restriction also states the population's count, sums and withholding, ranks by change and per partition. Implemented 2026-10-26 by the scale-limits change (PR #20) and live-validated on dev: where it differs from this decision, the amendment describes the code. ADR-152's acceptance is the owner's review.

> **Proposed amendment, 2026-10-25 (scale-limits r2a).** ADR-152 decision 7 would add a third bake kind to decision 1: `BAKE_GROUPED`, for an entity-streamed read, whose statement has no row `LIMIT`. Its finite bound is the sink's `max_kept` (`BAKE_MAX_ROWS`), and it is folded only, so no read holds an unbounded fetch.
## Context
Every page bake asks for at most 500 rows per question. That number is chat's narration rule: a
breakdown of 500 rows is "already more than a model can say anything true about". A compare wider
than its limit is refused, never truncated, so Retail health above 500 active products bakes no
products at all. The New customers gateway and the Audiences profile can do the same. Retail also
splits its product series into one query per day, 42 + 4 × days plans, only to fit that width.

The port's 10,000-row ceiling is also a narration rule, and it's why Campaigns chunks its daily
series by its campaign count. Both are chat rules acting as page bake widths.

Large sellers need 1,000–3,000 product rows, up to 15,000 product-by-channel rows, and up to 30,000
campaign-by-segment rows; daily series multiply those by days. These are projections from assumed
profiles.

Two things about how bakes read made any ceiling expensive:

- Results are paged through `GetQueryResults` into a list, so every row is held twice, and each
  1,000 rows costs one call against an account-wide rate.
- A batch executes every slot before shaping any, so every raw result of a batch is resident at
  once.

Every statement already asks for `LIMIT max_rows + 1` to detect truncation. An unbounded fetch
crashed dev on 2026-08-22, so a ceiling must stay finite.

## Decision
1. **Separate ceilings by caller kind.** The query port declares `NARRATION` (chat and
   request-time reads: 500 by default, 10,000 at most, unchanged) and two bake kinds:
   - `BAKE` for a held answer: `BAKE_MAX_ROWS`, proposed 50,000, set at 10,000 by the probe
     (decision 8);
   - `BAKE_STREAMED` for a reduction: the longest daily bake window (31 days) times
     `BAKE_MAX_ROWS`.

   The tool's wire input can't select either bake kind: it isn't a tool argument.
2. **Every bake read asks for a bake ceiling, as a constant.** No bake source imports chat's
   default, and no request's width is computed from results, so pin digests stay stable.
3. **Bakes read the result object, streamed.** The baker reads each statement's result object from
   its workgroup's result location in S3 as a stream, with the access it already has. Chat keeps
   paging. Each target names its transport, with no default, and the baker loses
   `athena:GetQueryResults`.
4. **Held answers and reductions.** A source declares each slot held (it keeps every row) or a
   reduction (rows stream through a closed fold that keeps a bounded summary: observed keys, keys by
   group, or per-subset daily sums). Every fold enforces the bound the memory model counts for it:
   keeping more is the same refusal as a truncated read. Folds reuse the domain's per-row rules and
   answer metadata, so withheld, currency, suppression and qualifier handling has one home. Each
   slot is shaped or folded in its own worker, so at most the batch width's raw results exist at
   once.
5. **Over the ceiling is a refusal, never a truncation.** A truncated bake answer is the isolated
   refusal `rows_over_ceiling` at the grain the read feeds (ADR-142).
6. **The ceiling is a memory-sized defect guard.** The build gate counts every byte a row holds:
   writes in flight, raw rows being shaped, every held answer, each fold's bound and the render's
   own structures. The probe measures the bytes per row and checks the streamed reader against the
   paged one. `BAKE_MAX_ROWS` is the largest of 50,000, 40,000, 30,000, 20,000 or 10,000 at which
   every row fits half the baker's memory. No query-time model enters it. It stops for the owner if
   nothing at or above 10,000 (the Campaigns product envelope) fits, if the readers disagree, or if
   a reused result's location lies outside the baker's result prefix. AWS documents result reuse
   as within one workgroup only, with a matching result configuration (Athena user guide, "Reuse
   query results in Athena", read 2026-10-18), and the bake workgroup enforces its output under
   the baker's prefix. The probe confirms that on dev: a statement run in the interactive
   workgroup and then in the bake workgroup must not reuse, while a same-workgroup control must.
   If it finds otherwise the owner chooses between no result reuse for bake reads (admission is
   still composed) and a wider read grant; no source switches to the streamed reader before the
   probe passes.
7. **Retail ranks its top 50 inside the query.** A ranked restriction, a bake-only planner
   capability that isn't a tool argument, ranks the population by current shipped revenue
   (descending, then product key) in SQL. One ranked read, the rank metric's own per-product
   read projected from the ranking, returns the 50 shown, a product with no revenue included at
   zero; its value orders the products and isn't displayed. One read per series and window
   returns daily rows for those 50 only. The rank rule has one home, that restriction, which
   renders both the SQL order and the order of the 50 rows it returns; the old Python sort over
   the whole catalog and the one-query-per-day split are deleted. The
   reads that need every product (the stated total, the product sums and the movers) keep the
   bake ceiling. Retail runs 47 plans at every preset and returns at most 50 × days series rows
   per read, at any catalog size.

8. **`BAKE_MAX_ROWS` = 10,000, a memory-sized defect guard, not a product bound.** Measured by
   the row-ceiling probe on dev on 2026-10-18 (09:08Z), at the commit that added the probe, before
   any bake read switched to the result object:
   - P1, local `tracemalloc`, the maximum over 3 runs at 10,000, 30,000 and 50,000 rows of the two
     widest held shapes: B_raw 987 B, B_shaped 930 B, B_kept 241 B per row; B_render 10,467 B per
     product (the Retail render's own structures). With W = 4,096,000 B and Q = 10, every roster
     row fits half of 1,024 MiB at 10,000 and not at 20,000. The binding row is
     `audiences#last_complete_month`: 4.1 MB + 98.7 MB + 288.3 MB + 0 + 104.7 MB = 495.8 MB of
     536.9 MB. The same terms derive 20,000 at 2,048 MiB.
   - P2a: the paged and streamed readers returned identical cells for a row of NULL, empty, quoted,
     comma, newline, non-ASCII, decimal and timestamp values; Athena writes NULL as an empty
     unquoted cell, which both read as absent.
   - P2b: 50,000 to 1,550,000 `UNNEST` rows streamed at 0.03 to 0.42 s per 100,000 rows.
   - P2c: a statement run in the interactive workgroup and then in the bake workgroup with reuse on
     did not reuse; the same-workgroup control did; every bake location was under the bake prefix.
   The stop rule fired on one term: today's Campaigns estate render costs 98,208 B per campaign
   excluding its encoded bodies, because Campaigns holds every rendered body until bodies stream
   (ADR-145). Campaigns keeps chat's ceilings, its 1,000-campaign guard and the whole-body memory
   check until then, so the orchestrator applied the owner's A3 default, the probe's value. The
   streamed-writes, grouped-Audiences and Campaigns changes re-run the probe and re-derive the
   value; one below 10,000 stops for the owner.
Decided by rubric item (1) of
[delivery practice](../delivery-practice.md#5-the-decision-weighting-rubric): pages showed nothing
for real catalogs because of a rule written for chat.

## Will be enforced by
- The query port's tests: each kind's bound, paired with an over-bound refusal; the default kind is
  `NARRATION`.
- The tool schema test: no ceiling-kind property, and a wire request for 50,000 rows refused.
- `tests/unit/test_athena_execution.py`: the streamed reader's NULL, quoting and header handling,
  identical rows from both transports, and a mid-stream failure as a dependency error.
- `tests/unit/test_fold_answer_parity.py`: folds give the same figures, units, suppression and
  truncation as a held answer; a fold on an ineligible plan is refused.
- `tests/unit/test_presentation_batch.py` and `tests/unit/test_athena_query_preflight.py`: a fold
  slot keeps only its summary; slots over one request run one statement, each shaped as it
  declared, whichever leads; a fold hands its sink the rows a held read keeps, on both transports.
- Each bake source's tests: requests carry a bake kind; a 600-product catalog bakes its top 50 with
  the total; Retail runs 47 plans at every preset, its ranked order equals the order captured
  before the change, and its series reads return at most 50 × days rows; a truncated answer maps
  to its declared scope.
- `tests/unit/test_folds.py`: a fold at its kept bound passes, and one entry past it reports
  truncated.
- The ranked restriction's tests: ineligible inputs refused; the composed statement's text is the
  same for a small and a large catalog; the ranked read returns every ranked product, revenue or
  not, and a catalog with fewer than 50 revenue-bearing products bakes without an invariant.
- `tests/unit/test_preset_bakeability.py`: the memory model's terms are printed and derived; the
  local estate tests at the certified widths stay within it.
- `validation/probe_bake_row_ceiling.py`: prints every term of the value.

## Consequences
- Retail health, the New customers gateway, the Audiences profile and Audiences' grouped campaign
  reads bake whole populations up to the ceiling.
- Retail's and Campaigns' plan counts stop growing with the window and the population.
- Chat's answers and limits are unchanged.
- Asking every bake read for its ceiling costs nothing on a small result.
- Bakes stop drawing on the account's `GetQueryResults` rate.
- The ceiling no longer depends on the 30 s-per-query model, which stays an alarm input only.

## Alternatives considered
- **Raise chat's default.** Rejected by (1): it changes chat, with wider answers the model can't
  narrate honestly.
- **Size the ceiling by fetch time against a per-query slot.** Rejected: the slot is a model, not a
  measurement. The owner's rule for this work is that every time budget and query count is a
  defect alarm, never a cut, so a per-query time can't size what a page shows.
- **Stream every product's series through a fold that keeps the 50 shown.** Superseded by
  decision 7: it reads every product's daily rows to keep 50, where the ranked read returns only
  the 50.
- **Filter series to chosen keys with an `IN` list built from results.** Rejected: the request
  would depend on a result, so the pin's request digests would change, and for Campaigns up to
  10,000 keys grows the statement toward Athena's 262,144-byte query-string limit. Ranking inside
  the SQL keeps the statement fixed.
- **Split wide reads over a closed axis** (channel or ad product). Rejected as the rule: it adds
  plans and can't cover campaign-by-segment, which has no closed axis.
- **No ceiling for bakes.** Rejected: an unbounded fetch crashed dev.

## Review trigger
A measured bake answer within 20% of the ceiling at a real seller. A baker memory change. Or a
Retail catalog within 20% of the ceiling, which would need the movers and the total computed in
the query too.
