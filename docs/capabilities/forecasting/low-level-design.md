# Forecasting — low-level design

This companion owns the proposed v1 forecast contract and the implementation boundaries required to
realize it. It does not authorize allocation optimization, causal claims, arbitrary request-time
scenarios, or a generic shared ML-artifact abstraction with saturation.

## 6. Interfaces and contracts

### Initial targets and series identity

V1 produces two independent baseline targets from independent governed historical series:

| Target | Basis | Initial use |
|---|---|---|
| `attributed_purchases` | Ad-attributed purchases at one exact governed scope | Expected conversion volume and variance under continuation of observed conditions. |
| `attributed_revenue` | Ad-attributed revenue at the same exact basis/scope, currency, and attribution window | Expected financial outcome with explicit currency and attribution semantics. |

`ForecastSeriesIdentity.v1` binds target, declared metric/basis, subject scope, marketplace, currency
where applicable, attribution selector/window, source-history policy version, and canonical input
provenance. Purchases and revenue never share a series identity, path, pointer, quality outcome, or
customer-facing label. Neither target is total retail sales, incremental impact, or a budget-allocation
outcome.

### Forecast history and model ports

The domain owns pure typed values such as `ForecastHistory`, `ForecastHistoryEligibility`,
`ForecastCandidate`, `ForecastGeneration`, `ForecastQuality`, and `ForecastServingState`. It accepts
already-authorized observations and never imports an AWS SDK, SageMaker type, Athena SQL, or page type.

| Port | Request responsibility | Result responsibility |
|---|---|---|
| `ForecastHistoryReadPort` | Read one already-bound governed series and its source provenance. | Ordered daily observations, coverage/suppression/restatement facts, and input provenance. |
| `ForecastModelExecutionPort` | Execute one eligible canonical history against pinned model/config identity. | Typed candidate path, backtest outputs, and execution state; no SDK types cross the port. |
| `ForecastGenerationStorePort` | Create an immutable accepted generation and conditionally advance its exact series pointer. | Applied, exact replay, stale-fence, conflict, or held outcome. |
| `ForecastArtifactReadPort` | Read an exact authorized series and closed horizon view. | Attested accepted generation plus fresh/stale/unavailable serving state. |

The batch coordinator first uses the existing `PublicationSelectionPort` to bind logical history plans to
exact CURRENT publications, then calls `ForecastHistoryReadPort`. It never builds SQL from a prompt,
queries an unselected publication, treats a gap or suppression as zero, or infers current availability
from a prior generation.

### Eligibility, candidate validation, and quality

An AMC-derived v1 series retains at most 365 continuous governed daily observations and requires at
least 180 complete days. The selected input end is the latest date whose required source coverage,
freshness, compatibility, suppression, and restatement rules permit use; it is never assumed to be
today.

Pure validation rejects:

- missing, duplicate, non-contiguous, incompatible, or excess-history dates;
- suppressed-as-zero replacement, unhandled coverage holes, or unsupported restatement treatment;
- candidate identity/provenance mismatch;
- a path other than exactly 90 contiguous future daily dates after the input coverage end;
- non-finite outputs, p10/p50/p90 order violations, target-aware unit/basis/currency violations, or
  output that exceeds the declared artifact bound; and
- a backtest candidate that fails an explicit versioned policy against declared baseline models.

Rolling-origin backtests may use only history available at that origin. Thresholds, comparison baselines,
and missingness/restatement policy remain explicit design inputs; they must not be invented in model
code or treated as a SageMaker default.

### Immutable generation and `CURRENT` pointer

The forecast artifact table mirrors the semantic publication durability model without reusing its
contracts. One exact series partition contains:

| Record | Mutability | Contents |
|---|---|---|
| `GENERATION#<id>` | Immutable | Validated 90-day path, input digest/provenance, target identity, model/config identity, quality/backtest state, and generation time. |
| `RUN#<id>` | Immutable | Bounded readiness, execution, or quality-hold outcome; never a raw history/model payload. |
| `CURRENT` | Conditionally mutable | Accepted generation identity, predecessor/fence state, and bounded serving freshness state. |

A series-scoped fenced lease or equivalent conditional predecessor token protects pointer advancement.
A publish transaction creates the immutable generation/run receipt and advances `CURRENT` only while the
expected fence/predecessor matches. Exact replay attests existing bytes; a conflict never overwrites a
generation. A failed/rejected run cannot delete or replace the last accepted generation.

Reads strongly attest `CURRENT` against its referenced immutable generation. If no accepted generation
exists, serving returns a typed unavailable state. If a newer run fails freshness/quality after an
accepted generation, serving returns that exact generation with typed stale state, age, and bounded
failure category.

### Staging and model package contract

The coordinator writes one canonical run input manifest under a private staging prefix. The manifest
contains run identity, series identity/digest, selected-input coverage end, and the canonical aggregate
observations needed by the Processing job. Candidate output uses a separate run prefix and must carry
the same identity/digest before the publisher reads it.

The exact Chronos model, model version, container image, package lock, artifact digest, processor input
schema, and processor output schema are mandatory implementation-time evidence. The Processing job may
use a pinned/approved package or model artifact, but it must not fetch mutable model code or weights
from the public internet at run time. A packaging/output compatibility spike must complete before the
stack or dependency change is accepted.

### Serving and governed-read contract

The API adds a dedicated protected forecast presentation route and `ForecastPresentationFunction`. The
browser never supplies a generation ID, raw series identity, scope key, model ID, or artifact payload.
The server resolves authorized identity, performs an exact forecast-artifact lookup, and returns a
closed presentation response for the fixed page.

The Forecast page is fixed-purpose and renders only supplied history, p10/p50/p90 interval, coverage
end, generation time, model/version label, quality/backtest state, stale/unavailable state, and the
non-causal baseline boundary. Forecast values are not modeled as observed `ShapedResponse` measurements.

A dedicated read-only forecast tool may make an admitted forecast artifact available to the
Conversational Runtime. The agent may explain uncertainty, basis, freshness, and its strategic
perspective; it cannot run inference, extend a horizon, calculate an interval, create a visual, or
reframe a continuation baseline as causal budget advice.

### Telemetry and acceptance

Forecast telemetry allowlists run state, target category, horizon, quality category, stale/fresh
boolean, bounded counts, durations, and model/config version. It excludes values, history rows,
forecast points, customer scope, raw artifact contents, input/output manifests, and full error messages.

Dev acceptance requires more than a successful deploy:

1. use a controlled synthetic series to prove model/package input and output compatibility;
2. run the deployed workflow under explicit dev identity and read its terminal state;
3. attest one immutable accepted generation and its exact `CURRENT` pointer;
4. call the authenticated forecast API and load the fixed browser page;
5. verify the protected forecast API returns the exact selected generation and the fixed browser page
   renders its supplied fields;
6. trigger a controlled eligibility or quality rejection; and
7. prove the last good artifact remains readable as stale while the held run and alarm category are
   observable.

Raw model input/output remains ignored local evidence. Published acceptance records only sanitized
conclusions and typed outcome counts.

### Read API

Decided by [ADR-163](../../decisions/adr-163-serve-demand-forecasts-and-status-through-a-read-api.md)
(2026-11-17). The weekly retail demand forecast is served by two read-only routes on the
authenticated HTTP API. The page and the four chat tools run one read service; it computes no
figure. The contract is [`read_api.py`](../../../src/domain/forecasting/read_api.py); its JSON Schema, route keys and closed
enums are generated into [`demand-forecast-api.json`](../../../contracts/generated/demand-forecast-api.json) by
`make generate-forecast-schemas`.

| Route | Query | Response |
|---|---|---|
| `GET /api/demand-forecast` | `status` (`shown` or a skip reason) and `page` (1–99,999) | `DemandForecastListV1`: one stored page of the ranked roster |
| `GET /api/demand-forecast` | `asin` alone | `DemandForecastDetailV1`: one product's stored bands, totals, accuracy and recent weeks |
| `GET /api/demand-forecast/status` | none | `DemandForecastStatusV1`: generation, freshness, last forecast run, hold, rollback, next checks |

- **Figures.** Each figure is `{value, display}`. `value` is the stored four-place string. `display`
  is the server's one rounding ([ADR-084](../../decisions/adr-084-display-monetary-values-at-currency-scale-from-a-server-display-value.md)):
  a whole number for unit targets, two places (half up) for `revenue_shipped` and `cogs_shipped`.
  The 4-, 8- and 13-week p50 totals are the publisher's stored roster row.
- **Instants** are whole-second UTC with `Z`. Dates are ISO `YYYY-MM-DD`.
- **Designed states** are 200 with an `outcome`: no generation (`unavailable`), a product not in
  the generation (`not_in_forecast`), all targets withheld (`not_forecast`), and an empty filter
  (page 1, no entries, `page_count` 1).
- **Freshness.** `stale` when age passes `stale_after_days` (16, the alarm's threshold and age
  function), or the last forecast run (`CURRENT.last_attempt`) failed or was held. The causes are
  `age_over_limit`, `last_run_failed` and `last_run_held`, sorted. `unavailable` when no generation.
- **Status.** `last_run` is `CURRENT.last_attempt`. `hold` is `quality` when that run was held, else
  `none`. `next_check_at` is the next daily check after now; `next_new_input_check_at` is the next
  check on the admission weekday, the only one that admits new input.

**Read plan.** Every read is a strongly consistent `GetItem`, keyed by the generation named in the
one `CURRENT` read. Round 2 runs concurrently and joins by key.

| Request | Round 1 | Round 2 | Items |
|---|---|---|---|
| list, page 1, no `status` | `CURRENT` | `META` | 2 |
| list, `status` or a later page | `CURRENT` | `META` ‖ `PAGE#` | 3 |
| detail | `CURRENT` | `ASIN#` | 2 |
| status | `CURRENT` | — | 1 |
| `forecast_list_asins`, no `asin`, no `status` | `CURRENT` | `META` | 2 |
| `forecast_list_asins` with `status` | `CURRENT` | `META` ‖ `PAGE#<filter>#00001` | 3 |
| `forecast_list_asins` with `asin` | `CURRENT` | `ASIN#` | 2 |
| query, summarize, compare | `CURRENT` | at most 5 `ASIN#` | at most 6 |
| no generation | `CURRENT` | — | 1 |

**Error codes.** Refusals carry `DemandForecastRefusalV1` (`refused`, `code`, `detail`). Headers are
`content-type: application/json` and `cache-control: no-store`.

| Code | `refused` | HTTP | Raised when |
|---|---|---|---|
| ADLY-FORECAST_READ-0001 | `ASIN_INVALID` | 400 | `asin` is not 10 letters or digits |
| ADLY-FORECAST_READ-0002 | `STATUS_INVALID` | 400 | `status` is not `shown` or a skip reason |
| ADLY-FORECAST_READ-0003 | `PAGE_INVALID` | 400 | `page` outside 1–99,999 (before any read), or above the filter's page count |
| ADLY-FORECAST_READ-0004 | `QUERY_INVALID` | 400 | an unknown or combined parameter |
| ADLY-FORECAST_READ-0005 | `FORECAST_ARGUMENT_INVALID` | tool | a tool argument fails its rule (`context.rule`) |
| ADLY-FORECAST_READ-0006 | `CURRENCY_MISMATCH` | tool | a money target over selected rows with more than one currency |
| ADLY-FORECAST_READ-0007 | `FORECAST_NOT_PUBLISHED` | tool | a tool call with no generation |
| ADLY-FORECAST_READ-0008 | `FORECAST_STORE_INCOMPLETE` | 503 | a missing `META`, an item that fails validation (including an `ASIN#` item with no stored row), or a page short against `META` |
| ADLY-FORECAST_READ-0009 | `FORECAST_STORE_UNAVAILABLE` | 503 | a DynamoDB client, connection or throttle error, or the request deadline (`context.cause`) |
| ADLY-FORECAST_READ-0010 | `ROUTE_UNKNOWN` | 404 | a route key this function doesn't serve |
| ADLY-FORECAST_READ-0011 | `FORECAST_READ_FAILED` | 500 | any other exception; `context.error_type` carries only the class name |
| ADLY-FORECAST_READ-0012 | `IDENTITY_REQUIRED` | 403 | the authorizer projected no session subject |

A missing reader setting is `CONFIG_UNRESOLVED` at init. Store faults and `FORECAST_READ_FAILED`
page through the error catch-all alarm; validation refusals do not.

**Deadlines.** Each request's deadline is the function's remaining time less a 1 s response
reserve. Each round runs in its own short-lived thread pool, shut down without waiting on every
exit, so a blocked read never holds a later request. The reader client makes one attempt per call
(`connect_timeout` 0.5 s, `read_timeout` 1.0 s, no retry), so two rounds take at most 3 s inside the
HTTP function's 10 s timeout. A throttle is not retried: it returns `FORECAST_STORE_UNAVAILABLE` and
the page offers Try again. The tool function's timeout is 15 s.

**Latency budgets** (server time p95: Lambda `Duration`, excluding `Init Duration`, cold and warm):
each route request at most 300 ms, and each tool call at most 300 ms, at 1,000, 10,000 and 50,000
ASINs. The item count per request doesn't change with catalogue size. Both are measured in the
deploy run (P3-1); a budget missed at 50,000 ASINs returns the slice to design.

### Demand forecast page
The Retail hub's Demand forecast tab (`/demand-forecast/`) is an `api_routes` navigation entry: it
has no artifact and calls the read API. The shell draws the one h1; the view draws none. Owner
decisions D1 to D7 of the 2026-11-20 design review (approved 2026-11-21) shape it:
- D1: the filter offers only the reasons that have products.
- D2: the list has no model column; the model is in the product's method lines.
- D3: no accuracy-class label; the product shows its measured range hits and average weekly miss.
- D4: status lives in the freshness pill's pop-up, with an out-of-date banner; there is no status panel.
- D5: the product view has a chart, drawn only from served values.
- D6: the product view shows one measure at a time.
- D7: no Previous or Next product control.

The hybrid layout of 2026-11-24 (owner decisions H1 to H5, `owner-decisions.md` "hybrid layout and
search") opens on a short list and one search box, shows one product at a time, and moves the full
list to Browse all.

1. **Scope card.** Pills for `item_count` products, the source, and freshness ("Up to date" or "Out of
   date", with sales through `input_end`). The pill's pop-up holds every status field: when the
   forecast was made and its `age_days`, the latest update or rollback, any hold, the next daily
   check, the next check that can use new sales data, and the `stale_after_days` rule. A week control
   from `horizon.views` shows the served window ("13 weeks starting Nov 8, 2026 · Sunday to Saturday
   weeks").
2. **Out-of-date banner.** When `freshness.state` is `stale`: one line per served cause, then what
   is shown and when new sales data can next be used.
3. **Answer sentence.** On the opening view, from the start list (the unfiltered page 1, never
   replaced by Browse all's filter or page) when its first row is rank 1 with a served ordered-units
   total: "{title or ASIN} leads the {default}-week forecast: customers are likely to order {total}
   units in the {V} weeks starting {date}". Otherwise the product-count fallback. An ask chip seeds
   the assistant with a dated question.
4. **Check these first and search (H1, H2).** "Check these first" lists the start list's ranks 1 to
   10 whose status target has a forecast, labelled "Largest forecast demand", never flagged: rank,
   name and ASIN, likely units ordered for the chosen weeks, and weeks shipped less than ordered of
   the last 13. It stacks below 560 px of the page container. A reasons line names each reason that
   has products, with Show them opening Browse all on that filter; then "Browse all {item_count}
   products". The one search box, "Find any product by ASIN", checks the format first: a short code
   gets the ASIN error, input with a space, over 10 characters or no digit gets "Searching by
   product name isn't available here". A valid ASIN shows "Finding…"; an edit or a new ASIN cancels
   it and its late answer is ignored. Not in this forecast, a read failure (Try again for the ASIN
   still in the box) and an invalid link answer on one line under the card header. Example ASINs
   come from the start list's generation.
5. **Browse all (H4).** An open entity, "All products", with Back. Chips read
   `counts_by_reason[status_target]` and name the status target's measure ("With an ordered-units
   forecast"); each requests `?status=` and renders the server's rows and `match_count`. The browser
   never filters. The ranked table: rank (by the default view, named in its header), product name
   and ASIN, units ordered, units shipped, shipped revenue and Amazon wholesale cost for the chosen
   view, and weeks shipped less than ordered of the last 13. A product with no forecast shows one
   "No forecast: {reason}" cell and a dash for its short-shipped count. The page column on this page is
   up to 1180 px, and `.demand-forecast` is its own `demand-forecast` size container: the list stacks
   below 912 px of that container, so it is wide at 1440 px with the assistant closed and stacks
   with it open. Then the pager and "How this
   forecast is made".
6. **Product, alone.** The product's name becomes the h1, with Back ("Back to all products" when
   opened from Browse all), and takes focus. Opening a product or Browse all pushes a history entry
   with `?asin=` or `?view=all`, so the browser's Back closes it; each write keeps every other
   `history.state` key. A fresh link gets an opening-view entry under it, a reload keeps its entry,
   and a duplicate, conflicting or invalid link opens the opening view with "This forecast link isn't
   valid." Back restores Browse all's filter, page and weeks and returns focus to what opened the
   view (row, search box, Browse all or Show them); Forward and links focus the h1. The crumb's and
   the active tab's "Demand forecast" open the opening view. A measure switch defaults to
   `status_target`. A forecast measure shows the answer sentence, four tiles (likely total, the
   view's last week with low and high, inside the range in testing, weeks shipped less than
   ordered), a chart of the recent weeks by state and the low-to-high band with no numeric axis,
   the dated table "Forecast, {V} weeks starting {date}, {measure}", the recent weeks with how each
   is known, and the method lines. A withheld measure shows the reason, its next step (dated from
   status, or its no-date form) and the recent weeks grouped by state. A link to an ASIN not in the
   forecast says so with the list's product count, and its button opens the opening view.
7. **Interruptions.** A publish between requests shows "A newer forecast was published while this
   page loaded" with Show the latest forecast. A read failure shows "The forecast can't be loaded
   right now" with Try again. A status failure leaves the page up and says so in the pop-up.

**Copy and binding rules.**
- Every figure renders its server `display`: money through `formatCurrency(display, currency_code)`,
  or `formatCanonicalDecimal` under a one-currency header, and units through `formatCount(display)`.
  A withheld money measure with `CURRENCY_MIXED` or no `currency_code` shows its week strip's dates
  and reporting states but no amounts, since no single currency labels them.
  The browser never rounds, sums or ranks; it picks served items by index, groups weeks by served
  state, places chart points and formats dates.
- No count the API doesn't serve: the week strip is headed "Recent weeks, {measure}", never a
  number of weeks.
- The "Weeks shipped less than ordered" pop-up says to review the history before a promotion when
  deciding whether the product needs a supply check, and that it shows neither inventory nor that a
  promotion is safe. It sets no threshold and classifies no product.
- Low, likely and high replace p10, p50 and p90; statistical names appear only inside info pop-ups.
- Every enum value has a plain-words entry; a test fails when a map's keys differ from the generated
  enum, so no internal code reaches the page. A failure is described by its family in words.
- A 400 `PAGE_INVALID` means a publish changed the page count: the view returns to page 1, refetches
  once and says so. An aborted request is not an error.
- The absence and error texts are catalog rows ABS-DF-1 to ABS-DF-3 in
  [page absence copy](../../ui/page-absence-copy.md).
## 7. Concepts applied

- [Data layer](../data-layer/README.md), [query path](../query-path/README.md), and publication selection own
  selected history and metric semantics.
- [UI page intent validation](../../ui/README.md) owns the dedicated forecast page's fixed visuals and
  state contract.
- [Conversational Runtime](../conversational-runtime/README.md) owns conversational publication from
  governed tool results; it does not consume a page artifact.
- [Agent Quality Loop](../agent-quality-loop/README.md) and [Observability](../../concepts/observability.md)
  own conversational quality linkage and protected evaluation custody, not numerical artifact publication.

## 8. Decisions

- [ADR-001](../../decisions/adr-001-chronos-deployment.md) — offline batch rather than request-time
  inference.
- ForecastDataStack separates retained forecast evidence from operational MlStack compute.
- Baseline forecast artifacts are product evidence with explicit uncertainty/quality state; they are not
  model opinion and must not be rewritten by chat.
- A later scenario grid is a separate non-causal capability; causal allocation needs a separately
  accepted data and identification contract.

## 9. Risks and open questions

- The exact Chronos model/version, packaging path, Processing image, and output schema remain
  unverified until the implementation spike.
- Backtest thresholds, baseline models, target-aware bounds, and missingness/restatement policy require
  product and statistical review before first publication.
- Forecast eligibility depends on customer-specific selected coverage; a local fixture cannot prove
  deployed customer-series quality.
- EventBridge Scheduler timing is an operational trigger, not proof that curation is complete; readiness
  remains an exact first workflow gate.
- The forecast page schema needs an explicit forecast artifact/mode rather than a fake observed-metric
  fit into the current analytical-page validator.
- Future scenario inputs require a governed planned-spend contract. The current data model does not
  provide comparable budget/pacing data, and no current capability supports allocation optimization.

AWS service behavior referenced by this design was rephrased from current official documentation for
compliance with licensing restrictions.
