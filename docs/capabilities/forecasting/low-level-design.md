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
has no artifact and calls the read API.

1. **Scope.** "Forecast from data through {input_end}, published {published_at}", a view control from
   `horizon.views` with `horizon.default_view_weeks` selected, labelled from `horizon.grain`, and the
   freshness banner when stale.
2. **Summary.** The model mix from the stored `counts_by_model` and `item_count`.
3. **Key outcomes.** Products not forecast, by reason, from the stored `counts_by_reason`.
4. **Visual.** None in v1; the weekly table is the exact-values form.
5. **Evidence.** The ranked table: rank, ASIN, title, demand (ordered units), shipped units, shipped
   revenue and COGS for the selected view, the model or the reason, and stock-out weeks in the last
   13. Then the ASIN search, the status filter and the pager.
6. **Detail, in place.** Per target: the model, the weekly p10/p50/p90 table for the view, the stored
   p50 total, the accuracy sentence ("the actual was inside the range in N of M test weeks"), the
   recent 13 weeks with their states and shipped-below-ordered flags, and the boundary: it continues
   observed conditions, isn't causal and excludes promotions.
7. **Status panel.** "Last forecast run", any hold or rollback, "Next check" and "Next check that can
   take new data".

**Copy and binding rules.**

- Every figure renders its server `display`: money through `formatCurrency(display, currency_code)`,
  units through `formatCount(display)`. The browser never rounds.
- Every enum value has a plain-words entry; a test fails when a map's keys differ from the generated
  enum, so no internal code reaches the page. A failure is described by its family in words.
- A 400 `PAGE_INVALID` means a publish changed the page count: the view returns to page 1, refetches
  once and says so. A 503, a 500 or a decode failure shows "The forecast can't be read right now"
  with Try again. An aborted request is not an error.
- No generation: "No forecast has been published yet", with the next new-data check date.

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
