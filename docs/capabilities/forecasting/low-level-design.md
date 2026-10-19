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
