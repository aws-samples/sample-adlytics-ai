# Performance: Growth check and Scorecard

See on one screen whether your Amazon advertising is growing the business, and which ad channel earns its budget.

One of the [Adlytics AI solutions](../../../README.md#solutions). It deploys with the full sample, which is sample code and not production-ready: read [Cost](../../../README.md#cost) and [Security considerations](../../../README.md#security-considerations) first.

## Who it's for

| Reader | Question | Decision |
|---|---|---|
| Brand or category manager | Is my advertising growing my business? Is my ad spend paying back, by channel? | Whether to act this week, and the budget split between channels |
| Finance or CMO | What did we spend, what came back, and did retail sales move with it? | Hold, grow or question the overall Amazon ad budget |
| Media buyer | Which channel pays back, and which wins first-time buyers? | The budget split between channels |

Every persona and view is listed in [who it is for](../../business-context.md#who-it-is-for).

## What you see

The screenshots come from a development deployment with synthetic data, in the light theme, captured in October 2026. Campaigns and figures are fictional. The synthetic data spends more on ads than it ships, so ad spend as a share of shipped sales reads 158.6%. To regenerate them, see [capturing screenshots](../../../validation/README.md#regenerating-readme-screenshots).

![Overview page for Sep 30 to Oct 6, 2026 compared with Sep 23 to 29: the Growth check tab and its question Is your advertising growing your business?, an answer sentence giving Amazon Ads' return for every 1 USD spent and the change in shipped retail sales and ad spend with the return's basis under it, tiles for ad spend, return on ad spend with each sale counted once, shipped revenue and ad spend as a share of shipped sales, and the top of two daily charts, with the assistant docked on the right](../../images/growth-check.png)

**Growth check, at `/`.** One sentence answers "is my advertising growing my business?": Amazon Ads returned 3.57 USD for every 1 USD spent, against 3.62 USD the week before, while shipped sales rose 4.0% and ad spend rose 5.6%. Each tile is the figure its source page serves, and both charts start at zero. See [Overview › Growth check](../../ui/overview/README.md).

![Scorecard under the Performance hub, with its question Is my ad spend paying back?, for the Amazon US marketplace in USD with 7 days, 30 days, Latest measured week and Latest measured month windows: an answer sentence giving the return for every 1 USD spent with each sale counted once, against the prior week, its basis, a How credit is shared disclosure, tiles for ad spend and for ad spend as a share of shipped sales with an Open Retail health link, a Spend across networks card reading Networks loaded: Amazon, and the top of the What each channel does for you cards, with the assistant docked on the right](../../images/scorecard.png)

**Scorecard.** The return counts each sale once, and its basis sits under it: first touch, 14-day, brand halo included. "How credit is shared", collapsed here, opens to compare the channel ranking under each attribution model. "Networks loaded: Amazon" means only one advertising network is loaded. See [Scorecard](../../ui/scorecard/README.md).

![What each channel does for you: one card each for Amazon DSP, Sponsored Products and Sponsored Brands with ad spend and its change, the channel's share of Amazon spend, return on ad spend, return including other products of the brand, the share of sales from first-time buyers and the ad cost per first-time-buyer purchase, and links to that channel's campaigns and to New customers, followed by notes on why channel figures aren't added together, a Show the channel table disclosure and a pointer to New customers](../../images/scorecard-by-channel.png)

**Channel cards, close-up.** Each channel's return sits beside its ad cost per first-time-buyer purchase, so a brand manager and a media buyer can agree a budget split from one screen. In this synthetic data the share of sales from first-time buyers is exactly 50.00%, with "No change", on all three channels; that is the dataset, not a rule of the page. See [channel cards](../../ui/scorecard/low-level-design.md#channel-cards-ui-refresh-23-readout-a3-od-ux-4).

## What it answers, and what it won't

| Ask it | Where |
|---|---|
| Is my advertising growing my business this week? | Growth check |
| What did Amazon Ads return for every 1 USD, with each sale counted once? | Scorecard headline ([ROAS definition](../../decisions/adr-017-roas-definition.md)) |
| Does the channel ranking change under another attribution model? | Scorecard, "How credit is shared" ([ADR-126](../../decisions/adr-126-render-the-scorecard-ranking-under-each-attribution-model.md)) |
| Which channel wins first-time buyers, and at what ad cost? | Channel cards |
| How much of shipped sales goes on ads? | Scorecard and Growth check tile (needs Vendor Central) |

What it won't do:

- Add attributed sales or returns across channels or networks, because one purchase can be credited to more than one channel ([ADR-106](../../decisions/adr-106-keep-outcome-metrics-inside-one-network-by-type.md)). Spend can be totalled.
- Credit retail sales to a campaign or channel: Vendor Central has no campaign dimension ([answerable questions](../../answerable-questions.md)).
- Show shipped sales, or ad spend as a share of them, in a customer deployment today. See "Load Vendor Central reports" in [Known limits](../../../README.md#known-limits).
- Other rows there that touch these pages: "Read every refreshed page without repeated or clipped copy", "Open the Scorecard and the Growth check right after updating…" and "See every page move to new data at the same moment".

## Data it uses

| Source | What it supplies | Grain and windows | Freshness |
|---|---|---|---|
| [Amazon Marketing Cloud](../../sources/amc.md) | Ad spend, impressions, Amazon-attributed sales and first-time-buyer sales, per channel | Daily, served as 7 days, 30 days, the latest complete week and the latest complete month; the Growth check shows the last 7 days | AMC data lands about 48 hours late and keeps 13 months; pages refresh when your pipeline lands a delivery |
| [Vendor Central](../../sources/vendor-central.md) | Shipped revenue, for the Growth check and the "ad spend as a share of shipped sales" tile | Daily | Can't be landed in a customer deployment yet |

Window rules are in the [temporal control contract](../../ui/temporal-control-contract.md).

## How it works

**Call-flow diagram — how the Growth check and Scorecard get their figures.**

![Call-flow diagram: how the Growth check and Scorecard get their figures, from your data to your browser. Your AMC pipeline, which you run from another AWS account, lands query results in the data lake, where they are checked against the data contracts. Each new version starts a page bake on AWS Lambda and Amazon Athena that computes the Scorecard for each window with the metric layer and stores the finished page. The sign-in and page API, on Amazon CloudFront, AWS WAF, Amazon Cognito and Lambda, serves the current page to the Scorecard in your browser, and serves four pages to the Growth check, which shows them on one screen. A dashed box shows Vendor Central reports, the source of shipped sales, which can't be landed in a customer deployment yet. Customer deployments refresh one page at a time; only the dev environment refreshes every affected page as one set.](diagrams/how-it-works.svg)

> **Takeaway.** Every figure is computed once, when data is published, by declared metrics; the browser only displays it.

- Your pipeline lands AMC query results. The [platform](../platform/README.md) checks each delivery against its data contract and publishes it as a new version.
- Each new version starts a bake that computes the Scorecard for every window and stores the finished page. A page that fails keeps its last good version. Customer deployments refresh one page at a time; only the dev environment refreshes affected pages as one set.
- The Growth check has no page of its own. It shows the served Scorecard, Retail health, New customers and Campaigns figures, so each figure equals its drill-down ([ADR-151](../../decisions/adr-151-compose-a-customer-tab-from-served-page-presentations.md)).
- The docked assistant reads the Scorecard's page context; see the [analytics assistant](../analytics-assistant/README.md).

## Deploy it

- It deploys with the full solution; there is no separate deploy. Follow the hub's [Quickstart](../../../README.md#quickstart) and [Deploy](../../../README.md#deploy), which use `make deploy-all`.
- The pages show designed empty states until your AMC deliveries land; [Load your data](../../../README.md#load-your-data) says how.
- Shipped sales stay empty until Vendor Central loading exists ([Known limits](../../../README.md#known-limits)).

## Adapt it

| To change | Edit | Then run |
|---|---|---|
| The Scorecard's sections, windows or tiles | Page `scorecard` in [`presentation/pages/v2/pages.yaml`](../../../presentation/pages/v2/pages.yaml) | The "page or preset view" row of [Extend it](../../../README.md#extend-it) |
| The Growth check's sources or question | The `/` entry under `composed_routes` in [`presentation/navigation.yaml`](../../../presentation/navigation.yaml) | The same row |
| A metric, such as another return basis | [`contracts/shared/metric-dictionary.yaml`](../../../contracts/shared/metric-dictionary.yaml) | The "metric" row |
| The Scorecard's insight card, a fixed rule with no prompt | [`contracts/insights/network-scorecard.yaml`](../../../contracts/insights/network-scorecard.yaml) | The "insight narration" row |

## Learn more

- Page designs: [Overview › Growth check](../../ui/overview/README.md), [Scorecard](../../ui/scorecard/README.md) and its [low-level design](../../ui/scorecard/low-level-design.md).
- Decisions: [ADR-017](../../decisions/adr-017-roas-definition.md), [ADR-106](../../decisions/adr-106-keep-outcome-metrics-inside-one-network-by-type.md), [ADR-126](../../decisions/adr-126-render-the-scorecard-ranking-under-each-attribution-model.md), [ADR-151](../../decisions/adr-151-compose-a-customer-tab-from-served-page-presentations.md).
- Related solutions: [Campaigns](../campaigns/README.md), [New customers and retention](../new-customers-and-retention/README.md), [Retail health](../retail-health/README.md), [Platform](../platform/README.md).
