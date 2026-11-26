# Demand forecast

> **A customer deployment can't show a forecast yet.** It can't load the Vendor Central history the forecast needs until the connector in slice 4. Only the dev environment, with synthetic data, has that history. See [Status](#status).

See how much each product will sell in a dated 4-, 8- or 13-week window starting on the forecast's first week, from its Vendor Central history, with a range around every week.

One of the [Adlytics AI solutions](../../../README.md#solutions). It deploys with the full sample, which is sample code and not production-ready: read [Cost](../../../README.md#cost) and [Security considerations](../../../README.md#security-considerations) first. It runs on the shared [platform](../platform/README.md) and reads the same Vendor Central data as [Retail health](../retail-health/README.md).

## Who it's for

| Reader | Question | Decision it leads to |
|---|---|---|
| Brand or category manager | How much will each product sell in the 4, 8 or 13 weeks starting on the date shown? | Which products to push, and what to raise with Amazon |
| Vendor or supply planner | How many units will Amazon order and ship, and how wide is the range? | How much stock to plan for Amazon's orders |
| Finance or CMO | What shipped revenue and Amazon wholesale cost does the dated forecast window hold? | The outlook for Amazon's wholesale spend, which is the vendor's revenue from Amazon |

Every persona and view is listed in [who it is for](../../business-context.md#who-it-is-for). The product is read-only: every decision is made in your own tools.
## What you see
These screenshots come from a development deployment with synthetic data, in the light theme, captured in November 2026. Products, ASINs and figures are fictional. To regenerate them, see [regenerating README screenshots](../../../validation/README.md#regenerating-readme-screenshots).
![Demand forecast tab under Retail health, with its question What demand should I plan for?: chips for 61 products, Vendor Central ordered and shipped, and Up to date, sales through Nov 7, 2026; 4 weeks, 8 weeks and 13 weeks with 13 weeks chosen, for the 13 weeks starting Nov 8, 2026; an answer sentence saying Northstar Home Product 36 leads the 13-week forecast and customers are likely to order 17,826 units; an Ask about this forecast button; and Check these first, with a Find any product by ASIN box and the products ranked 1 to 7 by 13-week likely units ordered, each shipping less than ordered in 13 of the last 13 weeks, with the assistant docked on the right](../../images/demand-forecast.png)
**Which products to plan for first.** The answer names the product that leads the 13-week forecast, and "Check these first" ranks the 10 products with the largest 13-week demand and shows how often each shipped less than ordered. The search box opens any product by its exact ASIN, and Browse all lists every product. See the [page design](../../capabilities/forecasting/low-level-design.md#demand-forecast-page).
![Product view for Northstar Home Product 36, ASIN B0FIXTUR36, opened from Check these first: Back to Demand forecast, the same freshness chip and week control, a measure control on Units ordered (demand), an answer sentence saying customers are likely to order 17,826 units in the 13 weeks starting Nov 8, 2026, tiles for the likely total of 17,826 units, the week of Jan 31, 2027 with likely 1,197 and a low of 1,027 and high of 1,495, 9 of 13 test weeks inside the range for the Chronos-2 forecasting model, and 13 of the last 13 weeks shipped less than ordered, then a chart of reported weeks from Aug 9, 2026 and the likely forecast with its low-to-high range to Jan 31, 2027, with the assistant docked on the right](../../images/demand-forecast-product.png)
**One product's forecast.** A product opens on its own link (`?asin=`), which the browser's Back closes, one measure at a time. Its tiles give the likely total, the last week's range, the test weeks inside the range with the model that made it, and how often it shipped less than ordered; the chart, weekly table and test detail follow.

## What it answers, and what it won't

What it answers:

- For each product and each week of a 13-week window starting on the served first week (for example "13 weeks starting Nov 8, 2026"): ordered units, shipped units, shipped revenue and Amazon wholesale cost, which is what Amazon pays the vendor for the units it shipped. Weeks of the window can already have passed when you read it.
- Each figure as a likely value with a low-to-high range (the 10th to 90th percentile forecasts), and its 4-, 8- or 13-week likely total.
- How accurate the shown model was: how many of its test weeks fell inside the range.
- Whether a product has a forecast, and if not, the reason it was withheld.
- Whether the forecast is fresh, when the last forecast run ended and how, and when the next check runs.

Where you see it:

- **The Demand forecast tab in the Retail hub** (`/demand-forecast/`). A one-line answer naming the product that leads demand, then "Check these first": the products ranked 1 to 10 by likely units ordered over the 13-week forecast (choosing 4 or 8 weeks changes the totals shown, not the order), each with how often it shipped less than ordered, beside one search box that finds any product by exact ASIN. Browse all lists every product ranked by forecast demand with all four measures, filter chips for the reasons that have products, and a pager. A freshness pill says whether the forecast is up to date, and its pop-up holds the update schedule. A product opens on its own, with its own link (`?asin=`) that the browser's Back closes, one measure at a time: its likely total, a chart of recent weeks and the forecast range, the dated weekly table, how it tested, and its recent weeks marked reported, counted as zero or not known. A product with no forecast says why and what to do next.
- **In chat.** Four tools read the same stored figures: `forecast_list_asins` (the top page, or one product by its ASIN), `forecast_query_asins` (weekly ranges for one to five products), `forecast_summarize_asin` (one product beside its recent history) and `forecast_compare_asins` (two to five products side by side).
- **Through the API.** `GET /api/demand-forecast` and `GET /api/demand-forecast/status`, open to any signed-in user ([read API](../../capabilities/forecasting/low-level-design.md#read-api)).

What it won't do:

- **Show a forecast that doesn't beat simpler methods.** Each product's forecast is backtested on its recent weeks against two simpler methods, SeasonalNaive and ETS. Chronos-2 is shown only where it beats both; otherwise the simpler method that tested best is shown. A product with no method that passed its checks has no forecast, and the page says why.
- **Say why a product has no recent sales.** "No recent sales" (`ASIN_INACTIVE`) means only that no sales were reported for the product in the 13 weeks of data the forecast checks. It doesn't say whether the product is still for sale or what its future demand will be, and since 2026-11-25 the chat tools state the reason in exactly those terms. Since 2026-11-26 they also serve the page's next step for every withheld reason, once per answer, and say it doesn't promise a forecast; each withheld product's line names its reason code, so every next step matches its products. Chat gets the page's undated wording ("The next weekly update tries again."); the page shows that only while its status is loading or failed, and otherwise names the update's date.
- **Say what a change would do.** The forecast projects from past weekly sales and product page views. It isn't a plan, and it doesn't say what a change in ad spend or price would cause.
- **Forecast ad-attributed purchases or revenue.** That is a separate forecast, proposed and not built ([What's coming next](../../../README.md#whats-coming-next)).
- **Compute a figure at read time.** The page, the tools and the API serve only the figures the forecast run stored; the assistant never computes, totals or estimates a figure ([ADR-163](../../decisions/adr-163-serve-demand-forecasts-and-status-through-a-read-api.md)).

## Status

As of 2026-11-19. Each slice ships on its own.

| Slice | What it adds | Status |
|---|---|---|
| 1 | The forecast engine and its weekly input | Merged. Accepted in dev on synthetic data on 2026-11-15 ([dated result](../../../validation/RESULTS.md#demand-forecast-slice-1-acceptance-on-landed-history-on-dev--2026-11-15)) |
| 2 | The publish gate, the forecast store, a weekly schedule, and an operator run and rollback | Merged. Accepted in dev on synthetic data on 2026-11-18 |
| 3 | The forecast page, chat tools, and a read-only forecast and forecast-status API | Merged. Accepted in dev on synthetic data on 2026-11-19 |
| 4 | A Vendor Central data connector (Selling Partner API, SP-API), so a customer deployment can load the history the forecast needs | Next |

## Data it uses

| Data | What it gives the forecast | Grain |
|---|---|---|
| Vendor Central sales report | Ordered and shipped units, shipped revenue and Amazon wholesale cost (`cogs_shipped`): the four forecast targets | Product, summed from days into weeks that start on Sunday |
| Vendor Central traffic report | Product page views, an input that informs the forecast but isn't forecast | Product and week |
| Vendor Central product hierarchy | Product titles | Product |

The policy, including how much history a product needs and how gaps are treated, is [`contracts/forecasting/demand-forecast.yaml`](../../../contracts/forecasting/demand-forecast.yaml). The source contracts are in [`contracts/vendor-central/`](../../../contracts/vendor-central/).

## How it works

**Call-flow diagram — how the Demand forecast page and chat tools get their figures.**

![Call-flow diagram: how the Demand forecast page and chat tools get their figures. Vendor Central sales and traffic history in the governed data lake feeds the weekly input, which a daily EventBridge Scheduler check starts and Step Functions and AWS Lambda stage per product; a customer deployment can't land Vendor Central reports yet, and the dev environment holds synthetic ones. SageMaker Processing jobs with no network access backtest and forecast each shard with Chronos-2, ETS and SeasonalNaive. The quality gate and publisher, on AWS Lambda, show Chronos-2 only where it beats both simple models, withhold the rest with a reason, and publish one immutable generation to the forecast store in Amazon DynamoDB under one CURRENT pointer. The forecast read function serves stored values through two GET routes on the HTTP API, behind the session authorizer, to the Demand forecast page in the signed-in user's browser. The forecast tool function runs the same read service behind AgentCore Gateway, which carries the user's identity, and returns the stored figures to the Amazon Bedrock AgentCore agent, whose answer appears in chat beside the page. Nothing is computed at read time; display rounding is the only arithmetic.](diagrams/demand-forecast.svg)

> **Takeaway.** Every forecast figure is computed once by the weekly run and gated before it is stored; the page and the assistant only read what was stored.

- **Weekly input.** A Lambda function reads the published sales and traffic from the data lake and stages each product's weekly history.
- **Forecast.** A Step Functions state machine starts SageMaker Processing jobs that run Chronos-2 with no network access, on model weights checked against a reviewed digest ([ADR-160](../../decisions/adr-160-mount-the-jumpstart-chronos-2-artifact-and-enforce-isolation-in-iam.md)). The same input and policy give the same forecast.
- **Gate (slice 2).** Each method forecasts a product's most recent weeks from the weeks before them. Chronos-2 is kept only where it beats SeasonalNaive and ETS.
- **Store (slice 2).** Forecasts that pass are published to a forecast store as one immutable generation, and a `CURRENT` pointer names the generation being served ([ADR-158](../../decisions/adr-158-store-demand-forecasts-as-immutable-generations-under-one-fenced-current.md)).
- **Serve (slice 3).** One read service reads only that store and returns stored values. The page calls it through the two API routes, and the chat tools run the same service in their own function, so the page and the assistant show the same figures ([ADR-163](../../decisions/adr-163-serve-demand-forecasts-and-status-through-a-read-api.md)).

## Known limits

- **A customer deployment has no Vendor Central history to forecast.** It can't load Vendor Central reports yet; see **Load Vendor Central reports** in the hub's [Known limits](../../../README.md#known-limits). Until slice 4, only the dev environment, with synthetic data, has that history.
- **Some products get no forecast.** A product with too little weekly history, or no Vendor Central data in recent weeks, is withheld with a reason.
- **One product's forecast can change when another joins or leaves the catalog.** Chronos-2 forecasts products in batches, jointly ([ADR-160](../../decisions/adr-160-mount-the-jumpstart-chronos-2-artifact-and-enforce-isolation-in-iam.md)).
- **Products are found by exact ASIN only.** There is no search by product name.
- **Chat doesn't page through the catalog.** `forecast_list_asins` returns the top page and finds any other product by its ASIN; the page and the API page through every product.
- **Weekly only.** There is no daily forecast.
- **A large catalog takes over an hour, and one run forecasts at most 200,000 products.** A 50,000-product run on dev with synthetic data published 81 min after its forecast jobs started, about 91 min with the input build's 600 s allowance [measured, dev, 2026-11-19]. Sizes and what happens past them: [Weekly demand forecast](../../../README.md#weekly-demand-forecast).

## Deploy it

- **It deploys with the full sample.** `make deploy-all` creates its two stacks, the read function in the API stack and the forecast tool in the agent stack. An EventBridge Scheduler schedule checks every day at 13:00 UTC; only the Thursday check admits a new weekly input, and a run starts SageMaker Processing jobs. Follow the hub's [Quickstart](../../../README.md#quickstart) and [Deploy](../../../README.md#deploy).
- **It needs a SageMaker Processing quota,** whose default is 0; see [Prerequisites](../../../README.md#prerequisites).
- **Its processing bucket and forecast table are retained** on teardown; see [Clean up](../../../README.md#clean-up).

### Test the weekly schedule without waiting

- **Start the scheduled path yourself.** Start the state machine `adlytics-<env>-demand-forecast` with the input `{"scheduled_time": "<UTC time>"}`, for example `2026-11-19T13:00:00Z`. The run takes its check date and run ID from that time, exactly as a Scheduler delivery does.
- **A future time is allowed.** It runs that day's check now, including the Thursday slot. The input build re-selects with today's date and refuses `SELECTION_MOVED` if the input differs, so nothing is published from data that isn't landed yet.
- **It takes that date's run ID, receipts and slot.** If the manual start published, the real Thursday delivery should then end `no_op_same_input` on the same input, or hold with `slot_already_consumed` on a new one.
- **A manual start that didn't publish can fail that date's real delivery.** If it ended `no_op_same_input` and the input then changes, the real delivery conflicts with its receipt, fails and raises `DemandForecastRunFailed`.
- **The real delivery still proves the eventing.** A manual start proves the run; only a Scheduler delivery proves the schedule, its role and its input. Read one delivery's execution after deploying.
- **`make demand-forecast-run` is different.** It starts an operator run for today, carries no `scheduled_time`, and isn't bound by the slot.

## Learn more

- Design: [Forecasting](../../capabilities/forecasting/README.md), its [read API](../../capabilities/forecasting/low-level-design.md#read-api) and [page](../../capabilities/forecasting/low-level-design.md#demand-forecast-page), [ADR-160](../../decisions/adr-160-mount-the-jumpstart-chronos-2-artifact-and-enforce-isolation-in-iam.md) and [ADR-163](../../decisions/adr-163-serve-demand-forecasts-and-status-through-a-read-api.md)
- Evidence: [demand forecast slice 1 acceptance](../../../validation/RESULTS.md#demand-forecast-slice-1-acceptance-on-landed-history-on-dev--2026-11-15)
- Source facts: [Vendor Central](../../sources/vendor-central.md)
- The whole solution: [Adlytics AI](../../../README.md) and the [platform](../platform/README.md)
