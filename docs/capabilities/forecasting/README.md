# Forecasting

| | |
|---|---|
| **Level** | L2 — capability overview |
| **Status** | Weekly retail demand forecast: built, not yet deployed outside dev or live-accepted. Ad-attributed baseline: proposed, review required before implementation |
| **Owns** | The weekly retail demand forecast and its read API; offline baseline forecast artifacts for ad-attributed purchases and ad-attributed revenue. For both: the serving contract, freshness, uncertainty, and quality gates. |
| **Conventions** | [Documentation conventions](../../conventions.md) |

> **Revision, 2026-11-17: the read API, page and tools are built.** Slice 3 serves the weekly retail
> demand forecast through two read-only routes on the authenticated API, the Retail hub's Demand
> forecast page and four chat tools, all over one read service that computes no figure
> ([ADR-163](../../decisions/adr-163-serve-demand-forecasts-and-status-through-a-read-api.md)). Not
> yet deployed or live-accepted; deploy and dev acceptance follow slice 2's merge.

> **Revision, 2026-11-16: weekly retail demand forecast gated and stored in dev, not shown.**
> Slice 1 added a weekly Vendor demand engine (Chronos-2 on ordered and shipped units, shipped
> revenue and COGS, 13 weeks ahead) and its weekly input in dev on synthetic data
> ([ADR-160](../../decisions/adr-160-mount-the-jumpstart-chronos-2-artifact-and-enforce-isolation-in-iam.md);
> [slice 1 acceptance](../../../validation/RESULTS.md#demand-forecast-slice-1-acceptance-on-landed-history-on-dev--2026-11-15)).
> Slice 2 adds the daily check, the quality gate, the publisher and the retained store with its
> fenced `CURRENT` pointer, the operator's start and rollback commands, and the forecast alarms.
> Planners see nothing yet: the page, tools and the rewrite of this package come in slice 3.

> **Revision proposed, 2026-10-07.** [ADR-092](../../decisions/adr-092-forecast-the-chronos-2-baseline-with-known-in-advance-covariates.md), [ADR-094](../../decisions/adr-094-run-offline-modelling-on-sagemaker-processing-without-a-vpc.md) and [ADR-114](../../decisions/adr-114-run-curve-and-forecast-jobs-as-execution-graph-workloads.md) (all Proposed) would add an internal `pacing_spend` target and a quantile-grid artifact, share the SageMaker Processing host with the curve jobs, and replace the Scheduler and Step Functions flow with execution-graph plans. See [Response curves and budget simulation](../../concepts/response-curves-and-budget-simulation.md). This package changes when they are accepted and built.

## 1. Purpose and scope

The **weekly retail demand forecast** gives a supply planner each product's expected demand on the
Vendor Central basis: ordered units, shipped units, shipped revenue and cost of goods shipped, 13
weeks ahead, with p10, p50 and p90 for every week. It runs offline, shows Chronos-2 only where it
beats both simple models on a backtest, and keeps the last good generation when a run fails or is
held ([ADR-156](../../decisions/adr-156-classify-forecast-weeks-as-observed-inferred-zero-or-unknown.md)
to [ADR-160](../../decisions/adr-160-mount-the-jumpstart-chronos-2-artifact-and-enforce-isolation-in-iam.md)).
It is read through the [read API](low-level-design.md#read-api) only.

The proposed **ad-attributed baseline** is a separate capability. Forecasting gives a practitioner an evidence-backed expectation of future ad-attributed purchases and
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
| **Responsibility** | Build and validate forecasts offline; retain the last valid generation; serve them by exact governed identity. Weekly retail demand: one fenced `CURRENT` per forecast set, read through two `GET` routes, the Demand forecast page and four `forecast_` chat tools. |
| **Inbound** | Selected daily canonical history, metric/basis/grain identity, coverage/suppression/restatement state, and model configuration. |
| **Outbound** | Weekly retail demand: per product and target, 13 weekly p10/p50/p90, the shown model and its backtest accuracy or the reason none is shown, recent weeks, and the generation's freshness and status. Ad-attributed (proposed): a daily p10/p50/p90 path with coverage end, generation time, model version and quality state. Both carry a typed unavailable or stale state. |
| **Owner** | Offline forecast job, forecast-artifact store, read adapter, and dedicated forecast page. |
| **Guarantee** | The product distinguishes forecast from observed evidence, attributes every path to a governed historical series, and never represents a baseline as causal budget advice. |

## Documentation map

- [Architecture](architecture.md) owns planned offline components and the daily publish-or-hold flow.
- [Low-level design](low-level-design.md) owns forecast targets, eligibility, artifact fields, horizons,
  refresh, quality gates, and serving constraints.
- [ADR-001](../../decisions/adr-001-chronos-deployment.md) owns the offline-batch decision rationale.
- [Read API](low-level-design.md#read-api) and [Demand forecast page](low-level-design.md#demand-forecast-page)
  in the low-level design own the weekly forecast's routes, error codes, deadlines and page copy.
- Weekly retail demand decisions: [ADR-156](../../decisions/adr-156-classify-forecast-weeks-as-observed-inferred-zero-or-unknown.md)
  (missing weeks), [ADR-157](../../decisions/adr-157-show-chronos-2-only-where-it-beats-both-simple-models.md)
  (model shown), [ADR-158](../../decisions/adr-158-store-demand-forecasts-as-immutable-generations-under-one-fenced-current.md)
  (store and pointer), [ADR-159](../../decisions/adr-159-orchestrate-the-demand-forecast-with-step-functions-until-adr-114.md)
  (orchestration), [ADR-160](../../decisions/adr-160-mount-the-jumpstart-chronos-2-artifact-and-enforce-isolation-in-iam.md)
  (model artifact and isolation) and [ADR-163](../../decisions/adr-163-serve-demand-forecasts-and-status-through-a-read-api.md)
  (read API).
- [Slice 3 delivery plan](slice-3-delivery-plan.md) owns the read API and page build order and its
  departures from the reviewed design.
