# Forecasting

| | |
|---|---|
| **Level** | L2 — capability overview |
| **Status** | Proposed — review required before implementation |
| **Owns** | Offline baseline forecast artifacts for ad-attributed purchases and ad-attributed revenue, their serving contract, freshness, uncertainty, and quality gates. |
| **Conventions** | [Documentation conventions](../../conventions.md) |

> **Revision proposed, 2026-10-07.** [ADR-092](../../decisions/adr-092-forecast-the-chronos-2-baseline-with-known-in-advance-covariates.md), [ADR-094](../../decisions/adr-094-run-offline-modelling-on-sagemaker-processing-without-a-vpc.md) and [ADR-114](../../decisions/adr-114-run-curve-and-forecast-jobs-as-execution-graph-workloads.md) (all Proposed) would add an internal `pacing_spend` target and a quantile-grid artifact, share the SageMaker Processing host with the curve jobs, and replace the Scheduler and Step Functions flow with execution-graph plans. See [Response curves and budget simulation](../../concepts/response-curves-and-budget-simulation.md). This package changes when they are accepted and built.

## 1. Purpose and scope

Forecasting gives a practitioner an evidence-backed expectation of future ad-attributed purchases and
ad-attributed revenue under continuation of observed conditions. It publishes daily p10/p50/p90 paths
for fixed 7-, 30-, and 90-day views after governed historical input passes eligibility and backtesting
gates.

It does not claim total retail demand, incrementality, causality, budget optimization, or a guaranteed
business outcome. It does not run model inference in a chat request. A future spend scenario is a
separate, explicitly non-causal capability and is not part of the initial release.

The [Data Layer](../data-layer/README.md) and [metric catalog/query path](../query-path/README.md) own
historical source and metric semantics. The page contract owns forecast visuals, and a governed
forecast read interface supplies any conversational use without permitting invented forecast values.

## 2. Where this fits

This capability refines the planned ML boundary in the L1 [architecture](../../architecture.md#capability-index).
It receives selected canonical history after curation, stores validated forecast artifacts, and exposes
them through a purpose-built forecast page and governed read interface.

## 3. Black-box contract

| | |
|---|---|
| **Responsibility** | Build and validate baseline forecast paths offline; retain the last valid generation; serve typed forecast artifacts by exact governed series identity. |
| **Inbound** | Selected daily canonical history, metric/basis/grain identity, coverage/suppression/restatement state, and model configuration. |
| **Outbound** | Exact forecast artifact with daily p10/p50/p90 path, input coverage end, generation time, model version, quality/backtest state, and typed unavailable/stale state. |
| **Owner** | Offline forecast job, forecast-artifact store, read adapter, and dedicated forecast page. |
| **Guarantee** | The product distinguishes forecast from observed evidence, attributes every path to a governed historical series, and never represents a baseline as causal budget advice. |

## Documentation map

- [Architecture](architecture.md) owns planned offline components and the daily publish-or-hold flow.
- [Low-level design](low-level-design.md) owns forecast targets, eligibility, artifact fields, horizons,
  refresh, quality gates, and serving constraints.
- [ADR-001](../../decisions/adr-001-chronos-deployment.md) owns the offline-batch decision rationale.
