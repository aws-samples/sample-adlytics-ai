# ADR-001 — Offline forecast deployment

| | |
|---|---|
| **Status** | Accepted — product target refinement pending implementation |
| **Date** | 2026-08-17; amended 2026-09-04 |
| **Decision** | Offline daily batch generation and retained forecast artifacts, not request-time inference. |
| **Related** | [Forecasting capability](../capabilities/forecasting/README.md) |

> **Proposed amendments, 2026-10-07.** [ADR-092](adr-092-forecast-the-chronos-2-baseline-with-known-in-advance-covariates.md) (Proposed) would add an internal `pacing_spend` target and store a quantile grid with a daily mean per series and day. [ADR-090](adr-090-evaluate-budget-scenarios-at-request-time-over-stored-draws.md) (Proposed) would answer arbitrary scenario inputs at request time, by arithmetic over stored draws, which this decision's review trigger names. [ADR-114](adr-114-run-curve-and-forecast-jobs-as-execution-graph-workloads.md) (Proposed) would run the daily job under the execution-graph control plane. The offline decision stands, and this decision stays in force, unchanged, until they are accepted.

## Context

Forecast inputs refresh through governed daily curation rather than a streaming path. A request-time
model invocation would normally read the same selected history as a scheduled job, while adding
interactive latency, capacity, and availability risk to chat and page rendering.

The initial product needs useful expectation-setting for ad practitioners without representing a
forecast as a causal budget-allocation recommendation. The target has two separate ad-attributed
outcomes—purchases and revenue—not total retail demand or one blended sales target.

## Decision

**Use offline daily batch inference and retain one validated 90-day daily path per exact forecast
series/generation. The dedicated page route and governed forecast read tool retrieve the stored
artifact; neither invokes a forecasting model on the customer request path.**

The initial artifact supports 7-, 30-, and 90-day views by slicing one retained path. It predicts
separate baseline targets for ad-attributed purchases and ad-attributed revenue from governed daily
history. It records exact target/basis/scope identity, coverage end, generation time, model version,
uncertainty path, history/quality state, and backtest outcome.

A generation publishes only after input eligibility and forecast-quality gates pass. On failure, the
last valid generation remains readable with a typed stale/unavailable state. The detailed contract,
including at-most-365-day AMC history, minimum 180 complete days, daily refresh after selected coverage,
and no causal allocation claim, is owned by the [Forecasting capability](../capabilities/forecasting/README.md).

The first release is a continuation baseline: it describes expected future attributed outcomes if
observed conditions continue. It does not forecast total sales, claim incrementality, recommend a
budget allocation, or treat observed spend/outcome association as causality.

## Consequences

- Forecasting adds no interactive model invocation to quick chat, deep dive, or page rendering.
- The 90-day retained path supports the agreed 7/30/90 views without fixed horizon-specific jobs.
- A new or insufficient series receives an explicit unavailable state instead of a fabricated forecast.
- The forecast page owns charts, tables, uncertainty display, freshness, and quality state; the agent
  may narrate a stored result and offer perspective but cannot produce a new forecast.
- A later predefined spend-scenario grid is a separate, explicitly non-causal enhancement. It requires
  a declared scenario-input contract and must not be rendered as budget optimization.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Request-time real-time endpoint | Adds availability/latency cost without new daily-source freshness. |
| Always-warm endpoint | Pays continuous capacity for bursty requests whose inputs refresh daily. |
| Generic model prose instead of a retained artifact | Cannot provide reproducible values, uncertainty, quality state, or a fixed page contract. |
| Direct budget allocation optimizer | Current facts do not establish comparable budget/pacing, causal effect, experimental identification, or allocation constraints. |
| One blended purchases/revenue/sales target | Blurs units, currency, basis, and the difference between attributed and retail outcomes. |

## Review trigger

Revisit this decision if data refresh becomes intra-day, arbitrary scenario inputs become a hard product
requirement, a causal/experimental allocation contract is accepted, advertised horizons exceed 90 days,
or forecast quality cannot beat agreed baseline models.
