# Demand forecast

> **In development: not yet available in a deployment.** No deployment publishes or shows a forecast yet. The engine runs in the dev environment on synthetic data; the page, chat tools and API come in a later slice. See [Status](#status).

See how much each product will sell over the next 13 weeks, from its Vendor Central history, with a range around every week.

One of the [Adlytics AI solutions](../../../README.md#solutions). It deploys with the full sample, which is sample code and not production-ready: read [Cost](../../../README.md#cost) and [Security considerations](../../../README.md#security-considerations) first. It runs on the shared [platform](../platform/README.md) and reads the same Vendor Central data as [Retail health](../retail-health/README.md).

## Who it's for

| Reader | Question | Decision it leads to |
|---|---|---|
| Brand or category manager | How much will each product sell over the next 13 weeks? | Which products to push, and what to raise with Amazon |
| Vendor or supply planner | How many units will Amazon order and ship, and how wide is the range? | How much stock to plan for Amazon's orders |
| Finance or CMO | What shipped revenue and cost of goods do the next 13 weeks hold? | The revenue and margin outlook |

Every persona and view is listed in [who it is for](../../business-context.md#who-it-is-for). The product is read-only: every decision is made in your own tools.

## What it will answer, and what it won't

What it will answer:

- For each product and each of the next 13 weeks: ordered units, shipped units, shipped revenue and shipped cost of goods (COGS).
- Each figure as a middle estimate (p50) with a p10 to p90 range.
- Whether a product has a forecast, and if not, the reason it was withheld.

What it won't do:

- **Show a forecast that doesn't beat simpler methods.** Each product's forecast is backtested on its recent weeks against two simpler methods, SeasonalNaive and ETS. A product is shown only where Chronos-2 beats both; otherwise its forecast is withheld with a reason.
- **Say what a change would do.** The forecast projects from past weekly sales and product page views. It isn't a plan, and it doesn't say what a change in ad spend or price would cause.
- **Forecast ad-attributed purchases or revenue.** That is a separate forecast, proposed and not built ([What's coming next](../../../README.md#whats-coming-next)).
- **Compute a forecast in chat.** The assistant will read stored forecasts through governed tools; it never computes or estimates a figure.

## Status

As of 2026-11-17. Each slice ships on its own.

| Slice | What it adds | Status |
|---|---|---|
| 1 | The forecast engine and its weekly input | Merged. Accepted in dev on synthetic data on 2026-11-15 ([dated result](../../../validation/RESULTS.md#demand-forecast-slice-1-acceptance-on-landed-history-on-dev--2026-11-15)) |
| 2 | The publish gate, the forecast store, a weekly schedule, and an operator run and rollback | In dev acceptance; not merged |
| 3 | The forecast page, chat tools, and a read-only forecast and forecast-status API | Next |
| 4 | A Vendor Central data connector (Selling Partner API, SP-API), so a customer deployment can load the history the forecast needs | Later |

## Data it uses

| Data | What it gives the forecast | Grain |
|---|---|---|
| Vendor Central sales report | Ordered and shipped units, shipped revenue and COGS: the four forecast targets | Product, summed from days into weeks that start on Sunday |
| Vendor Central traffic report | Product page views, an input that informs the forecast but isn't forecast | Product and week |
| Vendor Central product hierarchy | Product titles | Product |

The policy, including how much history a product needs and how gaps are treated, is [`contracts/forecasting/demand-forecast.yaml`](../../../contracts/forecasting/demand-forecast.yaml). The source contracts are in [`contracts/vendor-central/`](../../../contracts/vendor-central/).

## How it works

- **Weekly input.** A Lambda function reads the published sales and traffic from the data lake and stages each product's weekly history.
- **Forecast.** A Step Functions state machine starts SageMaker Processing jobs that run Chronos-2 with no network access, on model weights checked against a reviewed digest ([ADR-160](../../decisions/adr-160-mount-the-jumpstart-chronos-2-artifact-and-enforce-isolation-in-iam.md)). The same input and policy give the same forecast.
- **Gate (slice 2).** Each method forecasts a product's most recent weeks from the weeks before them. Chronos-2 is kept only where it beats SeasonalNaive and ETS.
- **Serve (slices 2 and 3).** Forecasts that pass are published to a forecast store. The page, chat tools and API will read only that store, so every forecast figure comes from the job.

## Known limits

- **Nothing is shown yet.** No deployment publishes or serves a forecast until slices 2 and 3 are merged.
- **A customer deployment has no Vendor Central history to forecast.** It can't load Vendor Central reports yet; see **Load Vendor Central reports** in the hub's [Known limits](../../../README.md#known-limits). Until slice 4, only the dev environment, with synthetic data, has that history.
- **Some products get no forecast.** A product with too little weekly history, or no Vendor Central data in recent weeks, is withheld with a reason.
- **One product's forecast can change when another joins or leaves the catalog.** Chronos-2 forecasts products in batches, jointly ([ADR-160](../../decisions/adr-160-mount-the-jumpstart-chronos-2-artifact-and-enforce-isolation-in-iam.md)).
- **Weekly only.** There is no daily forecast.

## Deploy it

- **The engine already deploys with the full sample.** `make deploy-all` creates its two stacks; they run nothing on a schedule until slice 2. Follow the hub's [Quickstart](../../../README.md#quickstart) and [Deploy](../../../README.md#deploy).
- **It needs a SageMaker Processing quota,** whose default is 0; see [Prerequisites](../../../README.md#prerequisites).
- **Its processing bucket is retained** on teardown; see [Clean up](../../../README.md#clean-up).

## Learn more

- Design: [Forecasting](../../capabilities/forecasting/README.md), whose 2026-11-11 revision note covers this forecast (slice 3 rewrites that package), and [ADR-160](../../decisions/adr-160-mount-the-jumpstart-chronos-2-artifact-and-enforce-isolation-in-iam.md)
- Evidence: [demand forecast slice 1 acceptance](../../../validation/RESULTS.md#demand-forecast-slice-1-acceptance-on-landed-history-on-dev--2026-11-15)
- Source facts: [Vendor Central](../../sources/vendor-central.md)
- The whole solution: [Adlytics AI](../../../README.md) and the [platform](../platform/README.md)
