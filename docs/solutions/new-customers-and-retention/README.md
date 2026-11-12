# New customers and retention

See which ad channels win first-time buyers for your brand, what each one costs, and whether those customers come back, so you fund the acquisition that lasts.

One of the [Adlytics AI solutions](../../../README.md#solutions). It deploys with the full sample, which is sample code and not production-ready: read [Cost](../../../README.md#cost) and [Security considerations](../../../README.md#security-considerations) first. It runs on the shared [platform](../platform/README.md).

## Who it's for

| Page | Reader | Question | Decision it leads to |
|---|---|---|---|
| Customers › New customers (`/new-customers/`) | Brand or category manager | Are ads winning first-time buyers, and at what cost? | Which ad type to fund for new customers |
| Customers › Retention (`/retention/`) | Brand or category manager; finance or CMO | Do the customers we win come back? | Which acquisition to defend |

Each persona and view is described in [who it is for](../../business-context.md#who-it-is-for). The product is read-only: every decision is made in your own Amazon Ads tools.

## What you see

These screenshots come from a development deployment with synthetic data, in the light theme, captured in October 2026. Products and figures are fictional. To regenerate them, see [regenerating README screenshots](../../../validation/README.md#regenerating-readme-screenshots).

![New customers page on Latest measured week, Sep 28 to Oct 4: a note that calendar periods have no comparison, an answer sentence saying the week's figures didn't arrive and giving first-time purchases per channel, a table of new-to-brand purchases, revenue, revenue share and cost per new-to-brand purchase for Amazon DSP, Sponsored Products and Sponsored Brands, a New buyers card saying the week's figures didn't arrive with a See why link, and the start of Gateway products, with the assistant docked on the right](../../images/new-customers.png)

**Which channel wins first-time buyers, and at what cost.** The table gives each channel's new-to-brand purchases, revenue, revenue share and cost per new-to-brand purchase, for example 7,349 purchases at 16.32 USD each for Amazon DSP, and a note says why the channels aren't added together. The week's new-buyer figures didn't arrive in the synthetic data, so the answer sentence and the New buyers card say so instead of guessing. Every channel's revenue share reads exactly 50.00%, a property of the synthetic data. See [New customers](../../ui/new-customers/README.md) and [page absence copy](../../ui/page-absence-copy.md).

![Retention page with cohorts through September 2026: an answer sentence comparing the share of customers won by DSP only, by sponsored ads only and by both who bought again by quarter 3, a note that cohorts are grouped by quarter, a chart of the share who had bought again by quarters 1 to 3 for each group, a Show values disclosure, and the By cohort section with its purchase path and acquisition channel selectors and the top of the cohort table, with the assistant docked on the right](../../images/retention.png)

**Whether won customers come back, by the channel that won them.** By quarter 3, 37.1% of customers won by DSP only had bought again, against 37.1% won by sponsored ads only and 36.8% won by both; the groups are this close in the synthetic data, so the three lines overlap. A note says cohorts are grouped by quarter because too many monthly cells had too few shoppers. The By cohort table switches between a repeat after an ad and any repeat purchase. The assistant's starter question still asks about "month 3" ([Known limits](../../../README.md#known-limits)). See [Retention](../../ui/retention/README.md).

## What it answers, and what it won't

Example questions:

- Which channel brings the most new-to-brand purchases, and at what cost per purchase?
- How many distinct new buyers did the last complete week or month bring, and what did each cost?
- Which products do new-to-brand shoppers buy first?
- Do customers won by DSP only, sponsored ads only, or both buy again, and how soon?
- With an AMC Paid Features subscription: do they buy again by any route, not only after an ad?

What it won't do:

- **Add attributed figures across channels.** One purchase can be credited to more than one channel, so each figure stays inside its channel.
- **Show new buyers for a rolling window.** Distinct buyers are shown only for complete weeks and months ([ADR-100](../../decisions/adr-100-serve-periodic-metrics-only-for-complete-measured-periods.md)).
- **Print a number AMC withheld.** A cell with too few shoppers reads "Not available", never 0, and nothing describes an individual shopper.
- **Compare monthly and quarterly cohorts.** A vintage is one or the other. The standard path counts only repeats after an ad, so it understates true repeat; the paid path counts every Amazon purchase ([ADR-104](../../decisions/adr-104-define-acquisition-cohorts-by-amazons-new-to-brand-flag.md)).

Known limits that affect these pages, in the hub's [Known limits](../../../README.md#known-limits): **Filter by brand, or use Audiences and Retention outside the US** and **Read every refreshed page without repeated or clipped copy**.

## Data it uses

All of it comes from Amazon Marketing Cloud (AMC), through query packs in [`contracts/amc/query-packs/`](../../../contracts/amc/query-packs/) that your own pipeline runs.

| Data | What it gives these pages | Grain |
|---|---|---|
| Attributed conversions (`amc-attributed-conversion`) | New-to-brand purchases and revenue per channel, and gateway products | Day and ad product; 14-day attribution by conversion date; "new to brand" means no purchase from the brand in the previous 12 months |
| New-to-brand buyers (`amc-new-to-brand-buyers-platform`, `amc-new-to-brand-buyers-channel`) | Distinct new buyers and cost per new buyer | Complete ISO week and calendar month |
| Amazon Ads delivery | Ad spend, the cost side of each cost-per figure | Day and campaign |
| Acquisition cohorts (`amc-acquisition-cohorts`) | The cohort grid and repeat curve for repeats after an ad | Cohort month, or quarter when months have too few shoppers |
| Amazon Retail Purchases cohorts (`amc-acquisition-cohorts-arp`, Paid Features) | Any repeat purchase, not only after an ad | The same as the standard cohorts |

Pages change only when your pipeline lands a new delivery. AMC keeps 13 months of history, makes a day available about 48 hours after it ends, and can restate recent conversions ([AMC freshness and restatement](../../sources/amc.md#7-freshness-and-restatement)). Whether a paid feature is available is read from the deliveries themselves ([ADR-107](../../decisions/adr-107-derive-paid-feature-availability-from-delivery-evidence.md)).

## How it works

**Call-flow diagram — how the New customers and Retention pages get their figures, in a customer deployment.**

![Call-flow diagram: how the New customers and Retention pages get their figures, in a customer deployment. Your extraction pipeline, in a second AWS account, runs the declared Amazon Marketing Cloud queries for new-to-brand purchases, new buyers and acquisition cohorts, and lands each result in the S3 landing bucket, where it is checked against its data contract and published to the governed data lake as an immutable version. With a Paid Features subscription, the cohorts also count any repeat purchase. A new published version starts the page bake, which computes each figure with tested code on AWS Lambda and Amazon Athena and writes the built page, with figures or a designed absence, to the page store. Each insight card is a fixed-rule statement computed from those figures; no AI model writes the cards. The Presentation API serves each page's latest good version to the signed-in user. By default a deployment bakes one page at a time; with the opt-in operational status plane cut over to GRAPH mode, all affected pages are built as one set.](diagrams/new-customers-and-retention.svg)

> **Takeaway.** Every figure and insight statement on these pages is computed once, by tested code, when your data is published, and no AI model writes the cards. The docked assistant's answers are written by an AI model and follow the [no invented figures](../analytics-assistant/README.md#how-it-stays-trustworthy) rule.

- **Landing.** A delivery that fails its data contract is refused, not partly loaded. Landing, the data lake and the secure edge are shared with every solution and described in the [platform](../platform/README.md).
- **Cohorts.** A cohort is the shoppers whose first new-to-brand purchase, by Amazon's own flag, fell in one month, or one quarter when months have too few shoppers, and was credited to ads ([ADR-104](../../decisions/adr-104-define-acquisition-cohorts-by-amazons-new-to-brand-flag.md)).
- **Designed absence.** A section without the data it needs says what is missing and why, rather than showing a zero or a guess ([ADR-139](../../decisions/adr-139-serve-designed-absence-and-advance-pages-only-with-a-grantable-cohort.md)).
- **Refresh.** By default a deployment bakes one page at a time, and a page that fails keeps its last good version; building every affected page as one set needs the opt-in operational status plane and the operator's one-way [cutover](../../capabilities/operational-data-flow-status/operator-tools.md#cut-over-to-graph-mode) to `GRAPH` mode, live-accepted only in the dev environment ([preset artifact publisher](../../ui/preset-artifact-publisher.md#the-pipeline)).

## Deploy it

- **It deploys with the full solution.** There is no separate install: `make deploy-all` builds these pages with the rest. Follow the hub's [Quickstart](../../../README.md#quickstart) and [Deploy](../../../README.md#deploy).
- **Before the pages fill,** turn on producer onboarding and land the AMC query packs in the table above ([Load your data](../../../README.md#load-your-data)). Until then each page shows its designed empty state.
- **For any repeat purchase,** your AMC instance needs a Paid Features subscription with Amazon Retail Purchases. Without it, Retention shows repeats after an ad only.

## Adapt it

- **The pages.** The `new-customers` and `retention` entries in [`presentation/pages/v2/pages.yaml`](../../../presentation/pages/v2/pages.yaml) declare each tab, its windows and its assistant starter questions. Regenerate with the "A page or preset view" row of [Extend it](../../../README.md#extend-it).
- **The insight cards.** [`portfolio-acquisition.yaml`](../../../contracts/insights/portfolio-acquisition.yaml) and [`retention-shift.yaml`](../../../contracts/insights/retention-shift.yaml) declare fixed-rule cards with no prompt; edit the rule there and regenerate with the "Insight narration" row.
- **The queries.** The packs in [`contracts/amc/query-packs/`](../../../contracts/amc/query-packs/); see the "An AMC query or Vendor Central report" row.
- **The pattern.** Cohorts defined by the source's own flag, periodic metrics only for complete periods, and a designed absence in place of a guess carry over to any cohort or acquisition view.

## Learn more

- Page designs: [New customers](../../ui/new-customers/README.md), [Retention](../../ui/retention/README.md), [page absence copy](../../ui/page-absence-copy.md)
- Measurement rules: [business outcome measurement](../../concepts/business-outcome-measurement.md), [answerable questions](../../answerable-questions.md)
- Source facts: [Amazon Marketing Cloud](../../sources/amc.md)
- Decisions: [ADR-100](../../decisions/adr-100-serve-periodic-metrics-only-for-complete-measured-periods.md), [ADR-104](../../decisions/adr-104-define-acquisition-cohorts-by-amazons-new-to-brand-flag.md), [ADR-107](../../decisions/adr-107-derive-paid-feature-availability-from-delivery-evidence.md), [ADR-139](../../decisions/adr-139-serve-designed-absence-and-advance-pages-only-with-a-grantable-cohort.md)
- The whole solution: [Adlytics AI](../../../README.md) and the [platform](../platform/README.md)
