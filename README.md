# Adlytics AI

> **This is sample code, not a production-ready solution.** You deploy it into your own AWS account and you are responsible for the AWS charges it incurs and for the security review it needs before production use. See [Cost](#cost), [Security considerations](#security-considerations) and [Known limits](#known-limits).

Adlytics AI is retail-media analytics for brands that sell to Amazon as a first-party (1P) vendor. It runs in your own AWS account, reads your Amazon Marketing Cloud (AMC) advertising data, and lets your team explore it through answer-first pages and a chat assistant. Every figure comes from tested code over your published data; the assistant explains and compares those figures, or says plainly when a question can't be answered. Vendor Central retail data can't be loaded into a customer deployment yet ([Known limits](#known-limits)).

## The problem it solves

Amazon gives a 1P vendor two reporting systems that disagree by design:

- **Amazon Marketing Cloud** holds advertising data: who saw an ad, what it cost, and which sales Amazon attributes to it. You query it with SQL in a privacy-protected environment that drops rows describing too few shoppers.
- **Vendor Central** holds retail facts: what shipped, what Amazon paid, what came back. It has no advertising dimension, a different day boundary and a different revision schedule.

"Is my advertising actually growing the business?" needs both. Today that usually means exporting reports from two consoles and reconciling them in spreadsheets, where it is easy to get silently wrong: spend that arrives in units 1,000 times apart, suppressed rows that make totals stop adding up, distinct-shopper counts summed across days. This sample does that reconciliation once, in declared and tested mappings, so a brand manager can ask the question in a sentence and get an answer they can defend. The full problem statement is in [business context](docs/business-context.md).

**Who it is for:** brand managers at 1P vendors, and the analytics leads and agencies who work with them. **Who deploys it:** a builder on that team with an AWS account.

## What it answers, and what it refuses

What a deployment can answer depends on the Amazon access you bring:

| Your Amazon access | Questions it adds | Examples |
|---|---|---|
| Vendor Central reports | Retail performance (can't be loaded into a customer deployment yet; see [Known limits](#known-limits)) | What did I sell, what came back, how did glance views convert? |
| An AMC instance mapped to your advertising accounts | Advertising measurement and new customers | What was my return on ad spend (ROAS) by campaign, across display and sponsored ads? How many distinct shoppers saw my ads, and how often? How much of my business is new-to-brand? |
| An AMC Paid Features subscription | Audience profiles and every repeat purchase | Which Amazon audience segments do my buyers belong to? Do new customers buy again by any route, not only after an ad? |

It refuses, and explains why, when the data can't support an honest answer. Some questions can never be answered from these two sources: retail sales attributed to a specific campaign (Vendor Central has no campaign dimension), anything about an individual shopper, competitor activity, or a single merged revenue figure. The complete list, with the wording the assistant uses, is in [answerable questions](docs/answerable-questions.md).

## How it works

**Call-flow diagram — from your data delivery to an answer in the browser.**

![Call-flow diagram: from your data delivery to an answer in the browser. Your extraction pipeline runs the AMC SQL (Vendor Central reports can't be landed yet) and lands each result in an S3 landing bucket; ingestion validates it against the data contracts and publishes an immutable version to the data lake, which the measurement tools read. A browser question reaches the Conversation API, which checks it with Amazon Bedrock Guardrails and invokes the AgentCore agent as the signed-in user; the agent calls the tools through AgentCore Gateway, and its answer streams back to the browser with an audit record written before the stream ends.](diagrams/how-it-works.svg)

> **Takeaway.** The model chooses which tool to call and writes the explanation; every figure comes from a tool that computed it from your published data.

1. Your own pipeline runs the declared AMC SQL and lands each result in this deployment's S3 landing bucket. Vendor Central reports can't be landed yet; see [Known limits](#known-limits). Extraction stays outside the sample, so it never holds your Amazon credentials.
2. Ingestion validates each delivery against its data contract and publishes it as an immutable version. A delivery that fails validation is refused rather than partly loaded.
3. In chat, the Conversation API verifies the user, screens the question with Amazon Bedrock Guardrails, and invokes an agent hosted on Amazon Bedrock AgentCore. The user's identity travels with every call, and each tool verifies it.
4. The agent calls measurement and catalog tools through AgentCore Gateway. The tools compute figures with Athena and return them with any caveats (coverage, suppression, measurement basis), or a reason the question can't be answered. The answer streams to the browser, and an audit record of the tools used is written for every turn.

**Call-flow diagram — how a page refreshes after new data is published.**

![Call-flow diagram: how a page refreshes after new data is published, in a customer deployment. A new published version in the data lake starts one bake per page and window, which computes that page from the version with the measurement layer. When that page builds, with data or a designed absence, its own pointer advances to the new immutable artifact and the Presentation API serves it to the browser; when it fails, that page keeps serving its last good version, and a bake that keeps failing raises a CloudWatch alarm. Insight narration from Amazon Bedrock is attached to a page by its revision. A box labelled dev environment only notes that the dev environment instead builds every affected page as one set, whose pointers advance together or not at all.](diagrams/page-refresh.svg)

> **Takeaway.** A page never shows a half-built result: each page moves to new data once it builds, or stays on its last good version, and a page that keeps failing raises an alarm.

Pages are computed when data is published, so they load fast and every viewer sees the same figures. A page whose data can't be shown yet says what is missing and when to expect it. The bake pipeline is described in [preset artifact publisher](docs/ui/preset-artifact-publisher.md).

**Key terms used across the docs:**

| Term | Meaning |
|---|---|
| Governed figure | A number computed by a declared, tested tool from published data, never by the model. |
| Publication / `CURRENT` | An immutable, validated version of a dataset / the pointer to the version being served. |
| Data contract | The declared shape and meaning of one source report, in [`contracts/`](contracts/). |
| Designed absence | The message a page shows, instead of a blank or a guess, when its data can't be shown yet. |
| Calibration | An automated test run a prompt or guardrail must pass before it is activated. |

Architecture in depth: [architecture overview](docs/architecture.md), [conversational runtime](docs/capabilities/conversational-runtime/README.md), and the [documentation index](docs/README.md). Design decisions are recorded as ADRs in [`docs/decisions/`](docs/decisions/).

## The application

These screenshots come from a development deployment with synthetic data, in the light theme, captured in October 2026. Campaigns, products and figures are fictional; audience segment names follow their providers' own naming. The synthetic data spends more on ads than it ships, so ad spend as a share of shipped sales reads above 100% and a product's ad-credited sales can exceed its shipped sales. The assistant examples replay a scripted answer, not a recorded conversation. To regenerate them, see [`validation/capture_screenshots.mjs`](validation/README.md#regenerating-readme-screenshots).

Each view answers one question and leads to one decision. Brand managers are the primary readers. Media buyers, at an agency or in-house, use Campaigns, Reach and Audiences to scale, cut or retarget spend. Finance or a CMO reads the Growth check. Each customer page leads with one answer sentence that fills a fixed template from the figures shown below it. [Who each view is for](docs/business-context.md#who-it-is-for) lists every view with its reader, question and decision.

### Growth check

![Overview page for Sep 30 to Oct 6, 2026 compared with Sep 23 to 29: the Growth check tab and its question Is your advertising growing your business?, an answer sentence giving Amazon Ads' return for every 1 USD spent and the change in shipped retail sales and ad spend with the return's basis under it, tiles for ad spend, return on ad spend with each sale counted once, shipped revenue and ad spend as a share of shipped sales, and the top of two daily charts, with the assistant docked on the right](docs/images/growth-check.png)

The Growth check at `/` answers the question brand managers and CMOs ask first, "is my advertising growing my business?", on one screen: what the ads cost, what came back, and whether retail sales moved with them. From there they decide whether to act this week, and which page to open next. Every figure on it is the figure served on the Scorecard, Retail health, New customers or Campaigns page, every chart starts at zero, and the assistant uses the Scorecard's page context. See [Overview › Growth check](docs/ui/overview/README.md) and its [answer template](docs/ui/overview/README.md#3-answer-template).

### Scorecard

![Scorecard under the Performance hub, with its question Is my ad spend paying back?, for the Amazon US marketplace in USD with 7 days, 30 days, Latest measured week and Latest measured month windows: an answer sentence giving the return for every 1 USD spent with each sale counted once, against the prior week, its basis, a How credit is shared disclosure, tiles for ad spend and for ad spend as a share of shipped sales with an Open Retail health link, a Spend across networks card reading Networks loaded: Amazon, and the top of the What each channel does for you cards, with the assistant docked on the right](docs/images/scorecard.png)

The Scorecard answers the brand manager's question "is my ad spend paying back, by channel?" with each sale counted once, and shows whether the ranking depends on the attribution model, so a budget split doesn't rest on one model's view. See [scorecard](docs/ui/scorecard/README.md).

![What each channel does for you: one card each for Amazon DSP, Sponsored Products and Sponsored Brands with ad spend and its change, the channel's share of Amazon spend, return on ad spend, return including other products of the brand, the share of sales from first-time buyers and the ad cost per first-time-buyer purchase, and links to that channel's campaigns and to New customers, followed by notes on why channel figures aren't added together, a Show the channel table disclosure and a pointer to New customers](docs/images/scorecard-by-channel.png)

The channel cards put each channel's return beside what it costs to win a first-time buyer, so a brand manager and a media buyer can agree a budget split from one screen. The first-time-buyer figures are New customers' own served figures for the same window, matched to each channel through the governed channel mapping, and the channel table stays one click away. See [channel cards](docs/ui/scorecard/low-level-design.md#channel-cards-ui-refresh-23-readout-a3-od-ux-4).

### Campaigns

![Campaigns page under the Performance hub, with its question Which campaigns should get more or less?, for all channels with 1 day, 7 days, 14 days and 30 days windows and a note that the data runs through Oct 6, 2026, an answer sentence saying 60 campaigns spent 240,337.56 USD, up 5.6%, ad spend and impressions tiles with prior values and changes, a line asking to pick a channel to see sales and return, three insight cards and the top of the daily ad spend chart, with the assistant docked on the right](docs/images/campaign-portfolio.png)

Campaigns is the media buyer's view of every campaign in one window, for deciding what to scale, hold or cut in Amazon Ads. Under All channels it shows only what can be totalled, spend and impressions, and asks for a channel before it shows purchases and return, because one purchase can be credited to more than one channel. Only published windows can be chosen, and the note names the days and how recent the data is ([temporal control](docs/ui/temporal-control-contract.md)). See [campaign portfolio](docs/ui/campaign-portfolio/README.md).

![Daily ad spend chart for Sep 30 to Oct 6, 2026, with axis values from 0 USD, a note to choose a channel to see purchases and ROAS over time, and a Show exact values disclosure](docs/images/portfolio-trend-chart.png)

The trend chart plots the served daily values from zero, and the exact numbers are one click away for anyone who needs to cite them. See [campaign portfolio](docs/ui/campaign-portfolio/README.md).

![Metric focus dialog for ad spend: the window total and change against the aligned prior, a daily chart, peak day, trough day, largest day move and window total, the largest per-campaign drivers, an interpretation, and a deep analysis with what stands out, three candidate readings and what would tell them apart](docs/images/metric-focus-dialog.png)

Opening a tile explains the change: which campaigns drove it, how much the rest of the portfolio contributed, and a deep analysis that offers candidate readings and names the evidence that would separate them. See [metric focus composition](docs/ui/metric-focus-contract.md#2-composition).

![Insight focus dialog for the portfolio's Contribution insight, marked Reading: a narration naming the campaigns with the largest spend gains and declines, then a deep analysis with what stands out, three candidate readings and what would tell them apart](docs/images/insight-dialog.png)

Opening an insight card shows the full reading behind it, so a team can check the reasoning before it acts on it. The deep analysis offers more than one explanation and says which evidence would settle it. The narration reads only the figures the page already shows, and says so: every number in it is one of those figures or worked from them, such as a difference or a share. See [insight focus dialog](docs/ui/insight-presentation-contract.md#3-clickable-cards-and-the-insight-focus-dialog) and [visual narration](docs/ui/workspace-assistant.md#5-page-owned-visual-narration).

![Campaigns table sorted by ad spend: a Late data note that states how days are bucketed and then repeats the same staleness sentence once for each source load, a campaign search box, and the first three campaigns with ad spend, attributed ROAS, attributed purchases and click-through rate as a percentage, each with its change](docs/images/portfolio-campaigns-table.png)

The campaigns table ranks every campaign by the column you choose, so a media buyer can sort by return to find the campaigns to fix. The Late data note above it warns when a source's latest load is older than its staleness threshold, so a team knows recent days may be incomplete before it compares campaigns. In this image the note repeats that warning once for each stale source load; see [Known limits](#known-limits). See [campaign portfolio](docs/ui/campaign-portfolio/README.md).

![Campaign detail for a Sponsored Products campaign: its channel, marketplace and window, four insight cards (Trend shape and Next check with a reading, Efficiency drift and Delivery and outcome reading Nothing noteworthy), and tiles for attributed purchases, ROAS, ad spend, impressions, clicks, click-through rate, cost per click and cost per thousand impressions with prior values and changes, with the assistant docked on the right](docs/images/campaign-detail.png)

Opening a campaign shows that campaign alone for the same window, so a team can move from what changed in the portfolio to the campaign that drove it. The assistant's "Using this page" line follows it there. The same detail serves every channel. Delivery tiles show cost per click and per thousand impressions against the prior window, so a media buyer can tell a campaign that got more expensive from one that stopped converting. A card with nothing worth saying reads "Nothing noteworthy" ([insight card states](docs/ui/insight-presentation-contract.md#2-the-five-designed-card-states)). See [campaign portfolio](docs/ui/campaign-portfolio/README.md).

### Reach and audiences

![Reach and funnel page for September 2026 with week, month and quarter windows, the quarter reading Not available yet, an answer sentence saying Amazon DSP reached 438,444 shoppers the sponsored ads didn't, three overlap cards with their August 2026 values, one reading Not available, a proportion bar with each group's shopper count, a Show shopper counts disclosure, and the start of What they did next with its association-not-cause notice, with the assistant docked on the right](docs/images/reach.png)

Reach and funnel answers the media buyer's question "does DSP reach shoppers my sponsored ads miss?", which decides the role DSP plays in the plan. Cells with too few shoppers read "Not available" rather than a number AMC withheld. See [reach and funnel](docs/ui/reach-and-funnel/README.md).

![When more ads stop paying: the line Sales per ad dollar, all Amazon ads: 11.25× at 1 ad, 2.13× at 10 or more, a one-line association-not-cause notice with a Why this is association, not cause disclosure, the share who bought by number of ads seen from 1 ad to 10 or more drawn as bars from 0%, the return at each step under the bars, a Show values disclosure, and a per-channel return list reading Not available per channel](docs/images/reach-frequency.png)

Frequency shows how the purchaser rate and the return change with the number of ads a shopper saw, so a media buyer can see where more ads stop paying before setting a frequency cap. Each association visual says in one line that it shows association, not cause, and keeps the reasons one click away. See [reach and funnel](docs/ui/reach-and-funnel/README.md).

![Audiences page for September 2026: an answer sentence saying shoppers in a synthetic in-market running-shoes segment are 35.0% of buyers but 20.0% of shoppers reached, 1.75× as common among buyers, profile and buyer selectors, and an in-market table comparing each segment's share of buyers with its share of reached shoppers and an index, a note that some breakdowns had too few shoppers to show, with the assistant docked on the right](docs/images/audiences.png)

Audiences answers "who buys, compared with who we reach?", so a media buyer can see which audiences over-index before building one in Amazon DSP. See [audiences](docs/ui/audiences/README.md).

![The Where this campaign's sales came from disclosure opened to the DSP campaigns by segment card: Showing 7 of 30 campaigns, a campaign selector, and impressions, CPM and ROAS per segment for the selected campaign](docs/images/audiences-campaigns.png)

Per campaign, behind a disclosure under the segment profile, it shows how each segment performed. It shows at most 7 DSP campaigns today; see [Known limits](#known-limits). See [audiences](docs/ui/audiences/README.md).

### New customers and retention

![New customers page on Latest measured week, Sep 28 to Oct 4: a note that calendar periods have no comparison, an answer sentence saying the week's figures didn't arrive and giving first-time purchases per channel, a table of new-to-brand purchases, revenue, revenue share and cost per new-to-brand purchase for Amazon DSP, Sponsored Products and Sponsored Brands, a New buyers card saying the week's figures didn't arrive with a See why link, and the start of Gateway products, with the assistant docked on the right](docs/images/new-customers.png)

New customers answers "are my ads winning first-time buyers, and at what cost?", so a brand manager can fund the ad type that wins them. When a feed is missing, the answer says so rather than guess: here the week's new-buyer figures didn't arrive in the synthetic data, and the page still shows the new-to-brand purchases it has. When a section's data isn't complete, the page says so and why ([page absence copy](docs/ui/page-absence-copy.md)). See [new customers](docs/ui/new-customers/README.md).

![Retention page with cohorts through September 2026: an answer sentence comparing the share of customers won by DSP only, by sponsored ads only and by both who bought again by quarter 3, a note that cohorts are grouped by quarter, a chart of the share who had bought again by quarters 1 to 3 for each group, a Show values disclosure, and the By cohort section with its purchase path and acquisition channel selectors and the top of the cohort table, with the assistant docked on the right](docs/images/retention.png)

Retention answers "do the customers my ads win come back?", by the channel that won them, so a brand manager can decide which acquisition to defend. See [retention](docs/ui/retention/README.md).

### Retail health

![Retail health page for Sep 30 to Oct 6, 2026: 7 products matching Vendor Central totals, 1 day, 7 days and 14 days windows with 30 days Not available yet, an answer sentence saying Amazon shipped 151,582.76 USD, up 4.0%, Where sales leak tiles for fulfilment rate, units per 1,000 product page views and return rate, the Ads beside retail strip with Open Scorecard and Each product beside its ad credit links, and the top of the shipped revenue chart, with the assistant docked on the right](docs/images/retail-health.png)

Retail health answers "are my products selling, and where do sales leak?" from Vendor Central, reconciled to the vendor's reported totals, so a brand manager knows which shipping or returns problem to raise with Amazon. Its [Ads beside retail](docs/ui/retail-health/low-level-design.md#ads-beside-retail-ui-refresh-25-od-ux-13-added-2026-10-22) strip puts ad spend beside the same window's retail figures, and shows no return, because Vendor Central has no campaign dimension. In a customer deployment this page waits on Vendor Central loading; see [Known limits](#known-limits). See [retail health](docs/ui/retail-health/README.md).

![Product detail page for one fictional product with the same window control, Back to Retail health, 1 of 7 products with Previous and Next, and the product's own demand and conversion, sales and fulfilment, and traffic value and margin tiles](docs/images/retail-product-detail.png)

Selecting a product opens its own page with the same measures, so a retail team can find which product moved a Retail health figure, and Previous and Next step through the rest without going back to the list. See [retail health](docs/ui/retail-health/README.md).

![Products and ads tab under Retail health for Sep 30 to Oct 6, 2026: the same window control, an answer sentence saying 7 products shipped and Northstar Home Product 4 shipped most, 31,165.40 USD, up 4.8%, Amazon DSP, Sponsored Products and Sponsored Brands channel buttons, a note that the two systems never add up or divide into each other, and a table of each product's shipped sales and change, product page views, units per 1,000 page views, Amazon DSP ad-credited sales and purchases, and First-time purchases reading Not measured yet, with the assistant docked on the right](docs/images/products-and-ads.png)

Products and ads puts each product's Vendor Central sales beside the sales one ad channel credits to it, so a brand manager can see which products the ads lean on. The two figures sit side by side and are never added or divided, because they come from two systems with different bases; there is no per-product return. See [Products and ads](docs/ui/products-and-ads/README.md).

### Analytics assistant

| ![Assistant panel on the Campaigns page mid-answer: two bullets about campaigns that moved ROAS, a collapsed How this was answered, and a progress list with completed tool calls and one in progress](docs/images/chat-streamed-answer.png) | ![A finished assistant answer with three bullets and a note on ad-attributed purchases, How this was answered opened to three numbered steps, each a plain description with its governed tool name, and a Copy reference button](docs/images/chat-answer-trace.png) | ![Assistant panel with the finished answer, its collapsed How this was answered and Copy reference, then a declined question with the refusal message and its own Copy reference](docs/images/chat-refusal.png) |
|---|---|---|
| Illustrative: an answer streams in while the assistant shows each tool it calls. | Illustrative: "How this was answered" lists the governed tools the answer used. | Illustrative: a question screened out before it runs is declined, with a suggestion and a Copy reference button for support. |

The assistant answers "why did it move?" for everyone on the team, so they know what to look at first. It is told which page and window you're on, shown as its "Using this page" line, and each answer names the window it covers. An analyst can open "How this was answered" to see which governed tools it called, and "Copy reference" copies the turn's support reference without printing it. See [workspace assistant](docs/ui/workspace-assistant.md) and [conversational runtime](docs/capabilities/conversational-runtime/README.md).

### Prompt administration

![Prompt administration page listing each insight prompt slot with its active version and other versions, one marked calibration needs changes and others calibration passed](docs/images/prompt-administration.png)

For administrators: every prompt that writes insight text, with its active version and calibration status. The first slot's versions in these images are a fictional fixture, so no real author appears. These two images were captured on 2026-10-19, because the synthetic capture user isn't a prompt administrator, so their sidebar predates the Overview item. See [prompt administration](docs/ui/prompt-administration/README.md).

![Two prompt versions: version 25 marked Active and created before authorship was recorded, and version 27 marked Needs changes, created by operator@example.com, failing 1 of 7 calibration cases](docs/images/prompt-version-calibration.png)

A version can be activated only after its calibration passes. A version created since authorship recording began records who created it; an older one, such as version 25 here, says that its author wasn't recorded. See [prompt administration](docs/ui/prompt-administration/README.md).

## Prerequisites

| Requirement | Detail |
|---|---|
| AWS account and Region | One account you control that runs no other workloads; deployment into a shared account is unsupported ([O-RESULT-SHARED-ACCOUNT-001](docs/security-dispositions.md)). The sample synthesizes only in us-east-1, us-east-2, us-west-2, eu-central-1 and eu-west-1, the Regions it declares a [guardrail profile](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-cross-region-support.html) for ([`route-guardrail.ts`](infra/lib/constructs/agent/route-guardrail.ts)). It has been deployed and accepted only in us-east-1. |
| AWS credentials | A named, non-default AWS CLI profile with administrator-level permissions in that account and a Region set. The deployment creates IAM roles and policies. |
| Amazon Bedrock model access | Enable access in your Region to the three models `.env.example` names, including the one-time use-case form Anthropic models need ([Request access to models](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html), read 2026-10-19). They are US cross-Region inference profiles; in eu-central-1 or eu-west-1, replace each with the ID that `aws bedrock list-inference-profiles --profile <your-profile> --region <region> --type-equals SYSTEM_DEFINED` lists. The memory model has no fallback ([ADR-147](docs/decisions/adr-147-treat-long-term-memory-as-context-never-evidence.md)). |
| Athena concurrent queries | Page refreshes and chat share Athena's "Active DML queries" quota ([query path](docs/capabilities/query-path/architecture.md)). In Service Quotas, read the applied value in your Region and request an increase if it is below `batch_athena_budget` + `chat_reserve_active_dml` in [`contracts/operations/execution-graphs.yaml`](contracts/operations/execution-graphs.yaml) (61 at this release). |
| Lambda concurrency | One environment reserves 35 concurrent executions for its functions once producer onboarding is on (33 before), and Lambda always keeps 100 of the account's concurrency unreserved. The applied "Concurrent executions" quota in your Region must therefore be at least 135. Read it with `aws lambda get-account-settings --profile <your-profile>` (`AccountLimit.ConcurrentExecutions`) and request an increase in Service Quotas if it is below 135; otherwise the first stack that reserves concurrency fails to deploy. |
| Edge rate limit | The website's AWS WAF rule limits requests per source IP per five minutes (`ADLYTICS_EDGE_WAF_RATE_LIMIT_PER_FIVE_MINUTES` in [`.env.example`](.env.example), with a separate limit for sign-in). A team behind one corporate IP shares that one budget, and each page view makes several requests. Size it for your team's combined traffic before you invite users. |
| Local tools | Node.js 24 (`.nvmrc`), Python 3.14 through [uv](https://docs.astral.sh/uv/), AWS CLI v2, `make`, `bash` and the `file` utility. Developed and deployed from macOS. Docker is not needed. |
| Amazon data (needed to see figures) | A deployment shows only designed empty pages until your own pipeline, run from a second AWS account, lands AMC query-pack results ([Load your data](#load-your-data)). Vendor Central reports can't be landed yet ([Known limits](#known-limits)). |

## Quickstart

Complete the [Prerequisites](#prerequisites) first; the command doesn't check model access or quotas. A Lambda quota below the minimum fails the first stack that reserves concurrency, and a model without access is expected to leave chat unable to answer.

```bash
cp .env.example .env                                   # fill in the blanks; each value is explained in the file
make deploy-all PROFILE=<your-profile> ENV=<env>       # shows the account, Region and changes; deploys nothing
make deploy-all PROFILE=<your-profile> ENV=<env> APPROVE=1 EMAIL=<you@example.com>
```

The command prints the website URL, and the invited user receives a temporary password by email. When you are done:

```bash
make destroy-all PROFILE=<your-profile> ENV=<env> CONFIRM=<env>
```

`destroy-all` keeps resources that hold data, which keep billing until you delete them; see [Clean up](#clean-up).

## Deploy

1. **Configure.** Copy `.env.example` to `.env` and fill in the blanks. Every value is explained in the file.
2. **Deploy everything.** Run `make deploy-all` without `APPROVE` first: it deploys nothing and shows the target account, Region and the changes. Then run it with `APPROVE=1 EMAIL=<address>` to deploy and invite the first user. What the target decides for you, such as CDK bootstrapping and the frontend's second pass on a first deployment, is listed in the header of [`scripts/deploy-all.sh`](scripts/deploy-all.sh). If a step fails, fix the cause and run the same command again: stacks with no changes are skipped, and a user who is already invited is not sent a second invite. If CloudFormation then reports that a name already exists, a failed first create left a retained resource behind: delete it (see [Clean up](#clean-up)) or use a new `ENV`.
3. **Sign in.** Open the printed URL and set a new password at first sign-in. To invite more users: `make create-user PROFILE=<your-profile> ENV=<env> EMAIL=<address>`.

An update of an existing development environment with no stack changes took about 11 minutes end to end (2026-10-01). A first deployment of a new environment took about 42 minutes end to end (2026-10-19), in an account already CDK-bootstrapped and with model access enabled; it has not yet been run in a new account ([record](docs/capabilities/operational-data-flow-status/operator-tools.md#appendix-fresh-environment-deploy-and-destroy-2026-10-19)).

**Updating an existing environment.** Run the same two commands. `make deploy-all` doesn't stop for a one-time step between stacks; each is stated in the comment beside its stack in `DEPLOY_ORDER` ([`infra/lib/config/index.ts`](infra/lib/config/index.ts)), so read those comments before you update.
An update from a version without the sharded sibling index ([ADR-145](docs/decisions/adr-145-serve-ten-thousand-campaigns-with-a-sharded-roster-and-streamed-writes.md)) shows Campaigns and Retail health in their failed state, and raises an alarm, until each page's next bake. No command starts that bake yet, so deploy shortly before a delivery lands and check both pages after it ([publisher](docs/ui/preset-artifact-publisher.md#first-bake-and-recovery)).

To deploy one stack on its own, run `make deploy STACK=Adlytics-<Area>-<env> PROFILE=<your-profile> ENV=<env> APPROVE=1`; it deploys exactly that stack. It takes no deploy lock, and it isn't for a `src/` change on a shared environment: every function-bearing stack shares one Lambda source asset, so deploy that change to every stack with `make deploy-all-locked` ([ADR-136](docs/decisions/adr-136-deploy-a-shared-asset-change-as-one-deploy-all-from-one-declared-source.md)).

### Check the deployment

- You can sign in with the invited user.
- The pages load and show their designed empty states. A new deployment has no data: its pages stay empty until you set up your data producers and their first deliveries land in the data lake. [Load your data](#load-your-data) says how.
- The chat assistant answers a question such as "What metrics can I ask about?".
- Subscribe to the alarm topic. Every alarm notifies one SNS topic, which the deployment creates with no subscriber, so no alarm reaches anyone until you add one:

  ```bash
  aws sns subscribe --profile <your-profile> --protocol email --notification-endpoint <you@example.com> \
    --topic-arn "$(aws ssm get-parameter --profile <your-profile> --name /adlytics/<env>/ingestion/alarm-topic-arn --query Parameter.Value --output text)"
  ```

  Confirm the subscription from the email AWS sends.

- After a deployment, up to three tamper-detection alarms (`adlytics-<env>-record-*`) can go to ALARM and return to OK on their own within about 15 minutes. They fire on a first deployment and on any deployment that changes a record table's policy or the functions it names.

### Optional features

All are off by default and are set in `.env`.

| Feature | Setting | Read first |
|---|---|---|
| Producer onboarding: a cross-account role your extraction pipeline assumes to land data | `ADLYTICS_PRODUCER_ONBOARDING_ENABLED` with the role ARN and external ID | [Source acquisition](docs/capabilities/source-acquisition/README.md) |
| Searchable traces (CloudWatch Transaction Search). An account-wide setting with its own charges. The agent runtime's span diagnostics need it | `ADLYTICS_TRANSACTION_SEARCH` | [Observability](docs/concepts/observability.md) |
| Online answer evaluation | `ADLYTICS_EVALUATION_INPUT_PROVISIONED` and related settings; parts are accepted only for an environment named `dev` | [Agent quality loop](docs/capabilities/agent-quality-loop/README.md) |

## Load your data

A new deployment starts with an empty data lake, so every page shows its designed empty state until data lands. The sample does not connect to Amazon itself: data arrives only from producers you set up, and only through producer onboarding, which is off by default.

To turn it on, set `ADLYTICS_PRODUCER_ONBOARDING_ENABLED=true` in `.env` with `ADLYTICS_LANDING_PIPELINE_ROLE_ARN`, one IAM role in a different AWS account from this deployment that your extraction pipeline runs as (synth refuses a role in the same account), and an `ADLYTICS_LANDING_EXTERNAL_ID` you choose. Then run `make deploy-all` again. The `Adlytics-Ingestion-<env>` stack outputs `OutLandingRoleArn`, the role your pipeline assumes with that external ID, and `OutLandingBucketName` and `OutLandingIncomingPrefix`, where it lands. For each AMC query pack in [`contracts/amc/query-packs/`](contracts/amc/query-packs/), your pipeline runs the exact SQL against your AMC instance, writes the result, then writes the registration object last. The [producer CLI](docs/capabilities/operational-data-flow-status/operator-tools.md#producer-cli) describes a pack, validates a result and renders the landing keys and registration for you. This registration handshake, described in [source acquisition](docs/capabilities/source-acquisition/architecture.md), is for the AMC query packs only: the reports the [`contracts/vendor-central/`](contracts/vendor-central/) contracts declare can't be landed through it yet; see [Known limits](#known-limits). For example deliveries of every contract, see the synthetic fixture generator in [development seeding](docs/concepts/dev-seeding.md).

## Extend it

**Call-flow diagram — how a change moves from a declared contract to a deployed environment.**

![Call-flow diagram: how a change moves from a declared contract to a deployed environment. You edit a declared contract, such as a metric, a data contract, a page or a prompt. A make generate target regenerates the schemas, fixtures and typed code that depend on it. The local gates make check and make verify refuse any drift between contract, code and docs. Only a passing tree is deployed, with make deploy-all, first as a diff and then with approval.](diagrams/extend-it.svg)

> **Takeaway.** You change what the product means in one declared place; generators and gates keep the code and docs in step with it.

| To add | Edit | Then run |
|---|---|---|
| A metric | [`contracts/shared/metric-dictionary.yaml`](contracts/shared/metric-dictionary.yaml) | `make check-lineage` |
| An AMC query or Vendor Central report | [`contracts/amc/`](contracts/amc/), [`contracts/vendor-central/`](contracts/vendor-central/) | `make validate-contracts`, `make generate-schemas`, `make generate-producer-packs`, `make generate-fixtures` |
| An agent tool | [`src/adapters/inbound/tool_handlers/schemas.py`](src/adapters/inbound/tool_handlers/schemas.py) and a handler beside it | `make generate-tool-schemas` |
| A page or preset view | [`presentation/pages/v2/pages.yaml`](presentation/pages/v2/pages.yaml), and for a tab its entry in [`presentation/navigation.yaml`](presentation/navigation.yaml) | `uv run python -m adapters.jobs.presentation_validation.generate --schema-version 2`, `uv run python -m adapters.jobs.presentation_validation.navigation`, then `make validate-page-schemas`, `make check-bakeability` |
| Insight narration | [`contracts/insights/`](contracts/insights/), [`contracts/prompts/insights/`](contracts/prompts/insights/) | `make generate-insight-matrix`; activate new prompt versions in Prompt administration |
| The agent's instructions | [`contracts/prompts/v1/managed-conversation.md`](contracts/prompts/v1/managed-conversation.md) | `make check-prompt-text`, then the agent regression in [validation](validation/README.md) |
| Different models | `ADLYTICS_*_MODEL_ID` in `.env` | `make deploy-all` |

Then run the gates [CONTRIBUTING](CONTRIBUTING.md#run-the-gates) lists for what you changed, and redeploy with `make deploy-all`. [Repository structure](docs/repo-structure.md) explains where everything lives.

## Build with an AI assistant

The repository ships guidance for AI coding assistants: [Kiro](https://kiro.dev/) loads `.kiro/steering/` automatically, and other assistants read [AGENTS.md](AGENTS.md). [Build with an AI assistant](docs/kiro.md) lists starter prompts, from a first deploy to tracing a figure, and how the guidance is organised.

## Known limits

What a seller can't do today, as of 2026-10-19; a row dated later says so. A fix described as designed, in review or in progress is not deployed yet; the linked document owns how the product behaves now. Sizes are from the code and from a development deployment with one synthetic dataset, not from a seller's real catalog.

| What you can't do | Who notices | Status |
|---|---|---|
| **Load Vendor Central reports.** Producer onboarding accepts only the AMC query packs, so a customer deployment has no way to receive Vendor Central data. Retail health, its product pages and Products and ads stay empty, and the Growth check and Scorecard show no shipped sales or ad spend as a share of shipped sales. | Every vendor | No fix designed yet (as of 2026-10-19). [Source acquisition](docs/capabilities/source-acquisition/architecture.md) |
| **Load a large daily delivery.** One day of vendor sales above about 840 active products fails to load. Product and campaign history grows with each delivery, so a smaller catalog reaches the limit over time. AMC campaign loads likely hit the same limit as campaign history grows, at a size not yet measured. | Large catalogs and long campaign histories first | Fix designed: load each unit from one staged file in a single set-based write. [Load path](docs/capabilities/load-path/README.md) |
| **See AMC history older than 13 months, or data fresher than your pipeline lands it.** AMC keeps 13 months of history, and pages refresh only when a delivery is published. | Everyone | AMC's own limit, not this sample's; history you land is kept. [AMC source facts](docs/sources/amc.md) |
| **See more than 7 DSP campaigns on Audiences.** The page says "Showing 7 of N", and chat can't rank across the rest. | Any seller with more than 7 DSP campaigns | Fix designed: one grouped read for every campaign, and a page per campaign fetched on selection. [Audiences design](docs/ui/audiences/low-level-design.md) |
| **See a list row or detail page past the top 50 products on Retail health.** The page works at any catalog size and its product count, sums and movers cover every product; only the top 50 by shipped revenue get a row and a detail page, by design. | Sellers with large catalogs | By design ([ADR-152](docs/decisions/adr-152-read-page-sized-answers-in-sql-with-no-arbitrary-row-ceiling.md)). [Retail health design](docs/ui/retail-health/low-level-design.md) |
| **See Campaigns above 1,000 campaigns.** The page is empty. | Large multi-brand sellers | Fix designed: a 10,000-campaign bound with streamed page writes. [Campaigns operational design](docs/ui/campaign-portfolio/operational-design.md) |
| **See Reach for "last quarter".** The option reads "Not available yet" even when the quarter's data has landed, because the page build refuses the quarter as too large. | Everyone | Fix chosen, not built yet: the quarter measured 184 day partitions against a bound of 100, so the page build will read a precomputed quarter manifest instead of every day partition ([ADR-146](docs/decisions/adr-146-publish-the-reach-quarter-or-retire-it-on-measured-partitions.md)). [Reach design](docs/ui/reach-and-funnel/low-level-design.md), [dated result](validation/RESULTS.md#d2-pages-and-graph-control-plane-live-acceptance-on-dev--2026-10-18) |
| **Filter by brand, or use Audiences and Retention outside the US.** Top lists mix brands, so the biggest brand dominates, and those two pages serve one marketplace. | Multi-brand and non-US sellers | Not designed yet; brand scope is a planned later capability. Workaround: the Campaigns name filter, or ask chat about a named product. [Answerable questions](docs/answerable-questions.md) |
| **Read the staleness note above the Campaigns table at a glance.** As of 2026-10-23, the Campaigns table no longer has a per-row Coverage column, but its one Late data note still repeats the same staleness sentence once for each stale source load, because each sentence names a different load time. Retail health's Products note does the same (seen on dev on 2026-10-22). Its first sentence also reads "in UTC, not by UTC". | Everyone who reads the Campaigns table or Retail health's products | Not fixed; the note shows each distinct served sentence once, and these differ only in their times. A single browser-owned line with the sentences in the Coverage sheet is the suggested fix. [Campaign portfolio design](docs/ui/campaign-portfolio/low-level-design.md), [Retail health design](docs/ui/retail-health/low-level-design.md#product-freshness-ui-refresh-ux-sweep-fix-2-2026-10-22-t2l-1) |
| **Read every refreshed page without repeated or clipped copy.** As of 2026-10-23: the Scorecard's channel cards are followed by notes and a pointer that repeat what the cards say, and its insight cards repeat each card's spend-share sentence; New customers repeats "No comparison for calendar periods." in its scope line and insight card; the Growth check's "What changed" shows the full served retail movers sentence (seen on dev on 2026-10-22); the Retention starter question asks about "month 3" while cohorts are quarterly; and, as of 2026-10-19, on a new deployment with no data yet the period picker says "Choose another available reporting window" and "Data as of" today's date, although nothing has been published. | Everyone | Copy and layout follow-ups, not yet fixed. Each figure shown is still the served figure. [Reach design](docs/ui/reach-and-funnel/low-level-design.md), [Scorecard design](docs/ui/scorecard/low-level-design.md), [Overview](docs/ui/overview/README.md) |
| **Rely on chat never restating a remembered figure.** Measured figures have been kept out of long-term memory since 2026-10-18 ([ADR-147](docs/decisions/adr-147-treat-long-term-memory-as-context-never-evidence.md)); live checks on 2026-10-18 and 2026-10-19 saw none restated, which is a few turns, not a rate. Memory can still hold a goal the user typed. | Everyone who uses chat | Fixed on dev 2026-10-18. [Dated result](validation/RESULTS.md#chat-long-term-memory-fix-adr-147-on-dev--2026-10-18) |
| **Ask chat for Amazon DSP cost per click.** As of 2026-10-19, a chat question about Amazon DSP cost per click may return no figure. | Everyone who uses chat for DSP campaigns | Campaign detail and the Campaigns page show it. A root fix in the query planner is planned. [Dated result](validation/RESULTS.md#amazon-dsp-campaign-detail-cost-per-click-on-dev--2026-10-19) |
| **Trust that a chat figure is for the channel you asked about.** On dev on 2026-10-19, after a refusal, chat answered two of five Sponsored Products questions with Amazon DSP figures. The figures were labelled DSP, but the heading above them did not name DSP. | Everyone who uses chat for sponsored channels | Not investigated; this is model and planner behaviour. Check the channel named next to each figure. [Dated result](validation/RESULTS.md#campaign-detail-and-deep-analysis-fix-on-dev--2026-10-19) |
| **Get fast answers to hard chat questions.** On dev on 2026-10-18, a full answer took a median of about 20 s and up to about 51 s at the 95th percentile (11 turns); model calls are most of that time. | Everyone | Being diagnosed; no fix is designed yet. [Dated result](validation/RESULTS.md#d2-pages-and-graph-control-plane-live-acceptance-on-dev--2026-10-18) |
| **Open the Scorecard and the Growth check right after updating an environment whose Scorecard was built before 2026-10-22.** That release added a field the Scorecard requires. An environment whose last Scorecard build predates it shows "This page cannot be shown safely right now" on the Scorecard and at `/` until a new publication rebuilds the Scorecard. In the set-at-once refresh there is no repository-owned command to request that rebuild. | Anyone updating an existing environment | Seen on dev on 2026-10-22 and worked around there by republishing data. A target-explicit rebuild command is an open decision. [Preset artifact publisher](docs/ui/preset-artifact-publisher.md#first-bake-and-recovery) |
| **See every page move to new data at the same moment.** Pages refresh one at a time, so for a short while after a publication two pages can show different publications. | Every customer deployment | Decided 2026-10-18: every deployment except the dev environment keeps the one-at-a-time refresh. The set-at-once refresh runs only in the dev environment, where it was accepted live on 2026-10-18; making it deployable everywhere and retiring the one-at-a-time path is the next planned change. The one-at-a-time path that customer deployments run is proven by unit and integration tests, not live, as of 2026-10-19: the fresh-environment deploy loads no data, so it bakes nothing. [Preset artifact publisher](docs/ui/preset-artifact-publisher.md#the-pipeline), [dated result](validation/RESULTS.md#d2-pages-and-graph-control-plane-live-acceptance-on-dev--2026-10-18) |
| **Trust every operations signal.** The errors-by-operation alarm can return to OK while a page is still broken, and a chat answer can't yet be tied to the insight it cited. | Operators | Alarm fix designed and pending; the chat-to-insight link has no fix built. [Dated result](validation/RESULTS.md#d2-pages-and-graph-control-plane-live-acceptance-on-dev--2026-10-18) |

What has been accepted, and when, is recorded in [validation results](validation/RESULTS.md); what is built and what is deferred is in [current state](docs/repo-structure.md#8-current-state). The sample includes no AMC or Vendor Central client; you bring the extraction pipeline. Several validation tools and optional modes are restricted to an environment named `dev`.

## What's coming next

These are designed or in progress, not built, and carry no dates. Every figure they add will come from new governed metrics, under the same rule as every figure today.

- **Forecasting:** a baseline forecast of ad-attributed purchases and revenue if nothing changes, with its uncertainty. [Forecasting](docs/capabilities/forecasting/README.md)
- **Budget simulation:** try a budget move against each campaign's response curve before making it in Amazon Ads; it changes nothing in Amazon. [Response curves and budget simulation](docs/concepts/response-curves-and-budget-simulation.md)
- **Amazon DSP's own forecasts as a second opinion:** shown beside the sample's curve, labelled as Amazon's and kept out of totals. [ADR-091](docs/decisions/adr-091-show-amazon-dsp-forecasts-side-by-side-and-gate-amazon-estimates-on-a-terms-record.md)
- **First-time buyers per product:** a governed per-product new-to-brand metric for the Products and ads First-time purchases column. [Products and ads](docs/ui/products-and-ads/README.md)
- **Other channels:** the data model can map data from other channels into the same structure, though only Amazon sources are loaded today. [ADR-052](docs/decisions/adr-052-separate-ecosystem-platform-channel-and-ad-product.md)

## Cost

You are responsible for the cost of the AWS services you use while running this sample. We recommend creating an [AWS Budget](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html) to monitor costs. Prices below are us-east-1, on-demand, without Free Tier or discounts, and are estimates, not a quote. Use the [AWS Pricing Calculator](https://calculator.aws/#/) for your Region and usage.

### While idle

Charged whether or not anyone uses the deployment. Counts are from the default configuration (22 stacks; the alarm count was re-read from a synthesized template on 2026-10-19); prices are from the AWS Price List API on 2026-10-01, and alarm billing per metric from the [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/) page on 2026-10-19.

| Service | What is deployed | Monthly |
|---|---|---:|
| AWS WAF | 3 web ACLs ($5 each) with 8 rules in total ($1 each) | $23.00 |
| CloudWatch alarms | 76 standard-resolution alarms, billed $0.10 per metric each alarm reads: 64 alarms on 73 metrics, plus 12 Metrics Insights query alarms billed per metric their query analyses (at least one each) | from $8.50 |
| AWS Secrets Manager | 3 secrets ($0.40 each) | $1.20 |
| AWS KMS | 1 customer-managed signing key | $1.00 |
| **Subtotal** | | **from $33.70** |

Add small charges for scheduled Lambda jobs, DynamoDB point-in-time recovery, S3 and CloudWatch Logs storage, and AgentCore Memory, all of which grow with use. [AWS WAF](https://aws.amazon.com/waf/pricing/), [CloudWatch](https://aws.amazon.com/cloudwatch/pricing/), [Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/), and [KMS](https://aws.amazon.com/kms/pricing/) pricing pages have current rates.

### With use

An illustrative model for chat turns only. Page insight narration also calls Bedrock: after each data publication, and when a user opens a deeper analysis.

| Input | Reference value |
|---|---:|
| Turn mix | 80% quick (2,000 input + 500 output tokens), 20% deep (8,000 input + 2,000 output tokens) |
| Illustrative model rate | $6 per million input tokens, $30 per million output tokens. Replace with the prices of the models you configure on [Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/). |
| AgentCore Runtime | One session per turn: 18 active vCPU-seconds and 125 GB-seconds (10 s at 1 GB, 20 s at 2 GB, 30 s at 2.5 GB), at $0.0895 per vCPU-hour and $0.00945 per GB-hour |
| AgentCore Gateway | Two operations per turn at $5 per million |
| Athena | 10 MB scanned per turn at $5 per TB |
| Bedrock Guardrails | One check per question, under 1,000 characters: $0.15 per 1,000 text units for content filters and $0.10 for sensitive information (2026-10-01) |

| Scenario | Turns per month | Bedrock | AgentCore Runtime | Gateway | Athena | Guardrails | Subtotal |
|---|---:|---:|---:|---:|---:|---:|---:|
| Trial | 100 | $4.32 | $0.08 | <$0.01 | $0.01 | $0.03 | **$4.43** |
| Team | 2,000 | $86.40 | $1.55 | $0.02 | $0.10 | $0.50 | **$88.57** |
| Department | 10,000 | $432.00 | $7.76 | $0.10 | $0.50 | $2.50 | **$442.86** |

Bedrock, AgentCore, Gateway, and Athena rates were checked on 2026-09-04. Not included: CloudFront, Cognito, Lambda, DynamoDB and S3 requests, CloudWatch Logs ingestion, data transfer, the optional features, and any AMC subscription. Monthly total = idle subtotal + usage subtotal + those.

## Security considerations

This sample has been reviewed and tested in a development account with synthetic data only. Before production use, review it against your own requirements. In particular:

| Area | What the sample does | What you should do for production |
|---|---|---|
| API endpoint | The HTTP API keeps its default `execute-api` hostname, which is publicly reachable. Every protected route requires a verified Cognito session, and requests that bypass CloudFront are refused ([edge entry](docs/concepts/authentication.md#edge-entry-adr-078)). | Serve the API from your own domain and disable the default endpoint. |
| Viewer TLS | The site uses CloudFront's default `*.cloudfront.net` certificate. CloudFront fixes its security policy at TLSv1, so browsers may connect with TLS 1.0 or 1.1. | Serve the site from your own domain with an ACM certificate, and set the distribution's minimum protocol to TLS 1.2 (`TLSv1.2_2021`) or later. |
| Encryption | S3 and Athena use S3-managed keys (SSE-S3); a customer-managed key mode is not available yet ([ADR-018](docs/decisions/adr-018-encryption-at-rest.md)). | Decide whether your data classification needs KMS customer-managed keys. |
| Data residency | Data stays in your account. Question screening uses a guardrail profile, and the example model IDs are cross-Region inference profiles; both may process a request in another Region of the same geography (US or EU). The edge WAF is in us-east-1. | Confirm this meets your residency requirements. Model IDs without a geography prefix keep inference in your Region where the model is offered there. |
| Diagnostic logs | The agent runtime's own log group keeps its runtime logs for 90 days. With `ADLYTICS_TRANSACTION_SEARCH` on, the runtime's spans, raw tool results included, go to the account-wide Transaction Search group `aws/spans`. Its retention is the account's Transaction Search setting, and with online evaluation on, the evaluation execution role can read it ([observability](docs/concepts/observability.md#34-protected-harness-diagnostics)). | Restrict who can read CloudWatch Logs in the account, `aws/spans` included, and decide whether that group's retention and readers fit your data. |
| Sign-in | Self sign-up is off; users are invited by an administrator. | Use your identity provider policies (MFA, password rules) as required. |
| Test tooling | The probes in [`validation/`](validation/README.md) are restricted to an environment named `dev` and use fictional data. | Do not treat sample test users or probes as production controls. |

Report a potential security issue through the [AWS vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/), not a public GitHub issue.

## Clean up

```bash
make destroy-all PROFILE=<your-profile> ENV=<env> CONFIRM=<env>
```

[`scripts/destroy-all.sh`](scripts/destroy-all.sh) states what it removes, what it keeps, and how it reports what is left. Add `INCLUDE_ACCOUNT_SETTINGS=1` to also remove `Adlytics-Observability-<env>`, which holds an account-wide tracing setting. Resources that hold data are retained on purpose and keep billing until you delete them by hand:

| Resource | Where | Before deleting |
|---|---|---|
| S3 buckets: raw deliveries, data lake, Athena results, published page artifacts | `Adlytics-DataV2-<env>`, `Adlytics-PublisherArtifactStore-<env>` | Empty them, including old object versions. |
| DynamoDB tables: publication state, insight cache, prompt registry, artifact pointers, audit records | DataV2, InsightCache, InsightPromptRegistry, PublisherArtifactStore, AgentQuality | None, except the two audit tables `adlytics-<env>-agent-quality-audit` and `adlytics-<env>-agent-quality-findings`: they are tamper-protected by design ([ADR-080](docs/decisions/adr-080-confine-each-audit-and-evaluation-record-family-to-one-writer.md)) and are kept. See [Remove the audit tables](#remove-the-audit-tables-optional). |
| Glue databases `adlytics_<env>_v2_source_aligned`, `adlytics_<env>_v2_candidate` and `adlytics_<env>_v2_current`, with their tables (`<env>` with each `-` written as `_`) | `Adlytics-DataV2-<env>`; untagged, so `destroy-all` lists them by name | None: `aws glue delete-database --profile <your-profile> --name <database>` deletes a database and its tables. |
| Cognito user pool, its client secret, and the review-token function and its log group | `Adlytics-Auth-<env>` | None. |
| AgentCore Memory (conversation history) | `Adlytics-Memory-<env>` | None. |
| Audit log group | `Adlytics-AgentQuality-<env>` | Deletion protection; turn it off first. |
| Agent runtime log group | `/aws/bedrock-agentcore/runtimes/harness_*` | Expires after 90 days, or delete it. |
| Optional features' data: evaluation evidence tables and result log groups, the evaluator, the operational status table | `Adlytics-EvaluationEvidence-<env>`, `Adlytics-EvaluationV4-<env>`, `Adlytics-OperationalStatusStore-<env>` | Result log groups have deletion protection; turn it off first. |
| Optional operational plane: the execution-graph tables | `Adlytics-ControlPlaneAuthorityStore-<env>` | Every table has deletion protection; turn it off first. |

Deploying the same environment name again fails while these exist, because several have fixed names: delete them first, or choose a new `ENV`.

### Remove the audit tables (optional)

The two audit tables bill on demand and cost close to nothing with no traffic, so you can leave them. Their resource policy lets only the deployment's CloudFormation execution role turn off deletion protection; [operator tools › remove the audit tables](docs/capabilities/operational-data-flow-status/operator-tools.md#remove-the-audit-tables) gives the import-and-delete procedure. Deleting them deletes every audit record.

The CDK bootstrap stacks (`CDKToolkit`) in your Region and us-east-1 are shared by any CDK app in the account; remove them only if nothing else uses them.

## Contributing

See [CONTRIBUTING](CONTRIBUTING.md), which lists the local gates.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
