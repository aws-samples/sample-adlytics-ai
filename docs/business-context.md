# Business context — what this unlocks

| | |
|---|---|
| **Type** | Primer. Assumes no technical background |
| **Status** | **Partial, deliberately.** §1–§5 are derived from what is built and verified. §6 lists the product questions that are still open, as questions rather than guesses |
| **Last updated** | 2026-10-24 |
| **Read next** | [understanding-the-data-model](understanding-the-data-model.md) for how it works · [answerable-questions](answerable-questions.md) for the capability surface |

## About the gaps in this document

§1–§5 state only what is built, verified or decided. Open product questions are in §6, as questions.

---

## 1. What the product is

**A conversational retail media intelligence platform for Amazon 1P advertisers, deployed into the customer's own AWS account.** A user asks a question in plain language and gets figures computed from their own data by deterministic code.

**It is an open-source template, which changes who decides what.** There is no hosted service and no shared environment. A customer deploys it against their own Amazon accounts, so **what any deployment can answer is a function of that customer's access, not of anything this template supplies or licenses** — see [answerable-questions](answerable-questions.md) §2a for the tiers. This is the same shape as [ADR-019](decisions/adr-019-grain-viability-is-measured.md), where a grain's viability is measured per customer rather than declared once for everyone: capability is a property of the deployment.

### Who it is for

**Brand managers, primarily.** Analytics leads and agencies working on a vendor's behalf are real secondary readers, and the boundary is genuinely moving — this work has traditionally sat with agencies, and first-party teams are increasingly trying to own insight rather than receive it. That shift is part of why a self-deployed tool is the right shape: it puts the analysis inside the vendor rather than behind a retainer.

Practically it means the product cannot assume a data specialist is reading it. A brand manager needs the answer and the caveat in a sentence, which is why the narration obligations in §5 are not decoration.

**Personas** (adopted 2026-10-22 with the UI refresh). Each one makes the decision in their own tools; the product is read-only.

| Persona | Top question | Decisions they make |
|---|---|---|
| **Brand or category manager (primary)** | Is my advertising actually growing my business? Are new customers coming in, and do they come back? | The channel budget split; which products to push or fix; what to raise with Amazon |
| **Media buyer**, at an agency or an in-house retail-media manager | Which campaign do I cut or scale? Does DSP reach shoppers search misses? Which audiences buy? | Scale, hold or cut campaigns; the DSP frequency cap; which audiences to build in Amazon DSP |
| **Finance or CMO** | What did we spend, what came back, and did retail sales move with it? | Hold, grow or question the overall Amazon ad budget |
| **Analyst** | Which basis, window and source is this, and why do two numbers differ? | Signing off figures before they go to finance or a client |

**Who each view is for.** Every built view answers one question that leads to one decision. Routes and tab names are owned by [`presentation/navigation.yaml`](../presentation/navigation.yaml); each view's design is in [UI page contracts](ui/README.md).

| View | Primary readers | Question | Decision it leads to |
|---|---|---|---|
| Overview › Growth check | Brand manager, finance or CMO | Is advertising growing my business? | Whether to act this week, and where |
| Performance › Scorecard, with its channel cards | Brand manager, media buyer | Is my ad spend paying back, by channel, and which channel wins new customers? | The budget split between channels |
| Performance › Campaigns | Media buyer | Which campaigns do I scale or fix? | Scale, hold or cut, in Amazon Ads |
| Reach and audiences › Reach and funnel | Media buyer | Does DSP reach shoppers sponsored ads miss, and when do more ads stop paying? | DSP's role and its frequency cap |
| Reach and audiences › Audiences | Media buyer | Who buys, compared with who we reach? | Which audience to build |
| Customers › New customers | Brand manager | Are ads winning first-time buyers, and at what cost? | Which ad type to fund for new customers |
| Customers › Retention | Brand manager, finance or CMO | Do the customers we win come back? | Which acquisition to defend |
| Retail health › Retail health | Brand manager | Are products selling, and where do sales leak? | What to raise with Amazon about shipping or returns |
| Retail health › Products and ads | Brand manager | Which products do my ads lean on? | Which products to advertise, feature or fix |
| Assistant | Everyone; the analyst opens its trace | Why did it move? | What to look at first |

Three properties define it, and each is a deliberate constraint rather than a feature:

| Property | What it means |
|---|---|
| **No figure is fabricated** | Every governed figure is computed by a tool. The language model chooses which tool to call and explains what it returns, stating only those figures and ones validly derived from them. With the optional online evaluation on, a sample of answers is scored against that evidence ([ADR-082](decisions/adr-082-govern-the-agent-by-evaluation-and-audit-not-text-heuristics.md)); answers outside the sample are not checked |
| **Single-tenant by deployment** | It is a template deployed into the customer's account, not a hosted service. There is no shared environment and customer data never leaves their account |
| **It refuses** | When a question cannot be answered honestly, the product says so and says why, rather than returning the nearest plausible number |

## 2. The problem being solved

Amazon gives a 1P vendor two separate reporting surfaces that do not agree, and no supported way to reason across them.

**Amazon Marketing Cloud** holds advertising data keyed on campaign and product: who saw an ad, what it cost, and which sales Amazon credits to it. It is queried by writing SQL against a privacy-constrained environment that withholds rows describing too few people, retains 13 months, and publishes no changelog.

**Vendor Central** holds retail truth keyed on product: what shipped, what it cost Amazon, what came back. It is a different pipeline with a different revision schedule, a fixed Pacific-time day boundary, and no advertising dimension at all.

**The gap.** Answering "is my advertising actually growing the business" needs both, and the two disagree by construction — one is attribution, the other is physical fact. Reconciling them is not a reporting task; it is a modelling task with several ways to get it silently wrong. The unlock is doing that work once, correctly, so a question can be asked in a sentence.

### What that looks like today

**Spreadsheets, and a lot of disparate reports.** A brand manager pulls exports from two Amazon consoles on different schedules, with different day boundaries and different definitions of revenue, and reconciles them by hand before any question can be answered.

Two things follow from that being the baseline rather than an incumbent product.

**The comparison is manual effort, not feature parity.** The bar is not "better dashboards than vendor X" but "the reconciliation step disappears."

**Every reconciliation someone does by hand is a chance to do it wrong**, and the specific ways are listed in §5. A person merging two spreadsheets has no equivalent of the divisor rules or the suppression flags — so the errors that table describes are not hypothetical risks the product avoids, they are errors the current process is probably already making, invisibly.

## 3. What it can answer today

The full list with grains and caveats is [answerable-questions](answerable-questions.md) §3. What any given deployment can answer depends on that customer's Amazon access; the tiers are [answerable-questions](answerable-questions.md) §2a. In business terms, four families:

| Family | Example questions |
|---|---|
| **Advertising efficiency** | What was my return on ad spend, by campaign? Across display and sponsored ads together? |
| **Advertising's wider effect** | What lift do my ads create on products I am not advertising? How much of my business is new customers? |
| **Retail performance** | What did I sell, what did Amazon pay me, what came back, what converted? |
| **Reach and frequency** | How many distinct shoppers saw my ads, and how often? |

Brand and parent-product rollups aren't available yet; [answerable questions](answerable-questions.md) §2a says why.

## 4. What it will never answer

These are not gaps in the build. They are properties of what Amazon provides, and knowing them matters more than knowing the capabilities — because a promise made against one of these cannot be kept later.

| Limit | Why it is permanent |
|---|---|
| **Actual retail sales attributed to a campaign** | Vendor Central has no campaign dimension at all. Advertising and retail can only be compared at product × date × marketplace, never finer |
| **Anything about an individual shopper** | Amazon's privacy model forbids person-level output structurally. All person-level work happens inside Amazon and emits only aggregates |
| **Demographics of your shoppers** | AMC has no age, income or marital-status field. Where Amazon DSP offers an age or gender audience segment, Audiences shows it as a segment like any other, never as a breakdown of your shoppers |
| **Competitor activity** | Neither source contains it |
| **A single reconciled revenue figure** | Ad-attributed and retail-actual revenue are different measurement systems. The product reports both and labels them; it will never merge them into one number |

The full list with the phrasing to use when refusing is [answerable-questions](answerable-questions.md) §6.

## 5. Why the trust posture is the differentiator

This is the part most worth understanding, because it explains where the engineering effort has gone.

**A language model asked to compute a number will occasionally be confidently wrong.** In a reporting product that is not a rough edge, it is disqualifying — one plausible wrong figure in a board deck costs more trust than fifty correct ones earn. So every governed figure comes from deterministic tools, never from the model: the tools compute them from the source data, where the failure modes below live. The agent then reasons over those figures as an analyst reads a report, deriving changes, shares, and labelled projections, and never fabricates one. Every turn writes an audit record of the tools it used, and with online evaluation on, a sample of answers is scored against the evidence they had ([ADR-082](decisions/adr-082-govern-the-agent-by-evaluation-and-audit-not-text-heuristics.md)).

**The failure modes being defended against are specific and invisible.** Each of these was found in Amazon's own documentation or by probing a live instance, and each produces a number that looks entirely reasonable:

| Trap | What it would have produced |
|---|---|
| The same spend concept arrives in units 1,000× apart on two Amazon platforms, and one table mixes both | Advertising fees understated by 1,000×, while the headline figure still reconciles |
| Applying a cost scaling factor to a revenue column | Return on ad spend wrong by five to eight orders of magnitude |
| Amazon silently deletes rows describing too few shoppers unless the query asks otherwise | Totals that quietly do not add up, with no gap, no marker and no error |
| Two near-identical Amazon tables, one of which extends its own window after the fact | The same question returning different numbers on different days |
| Two campaign identifier columns from different systems, both populated | Rows dropped silently rather than an error |
| Summing a distinct-user count across days | Reach overstated, with no way to detect it afterwards |

**The product's defensibility is that these are handled once, in a declared mapping, checked by a build gate** — rather than being handled correctly by whoever wrote the last query. Two gates enforce it: one validates every contract against a verified inventory of Amazon's columns, the other traces every column from source to metric and refuses a metric that claims a breakdown its own inputs cannot support.

**That second gate has already caught real defects.** The most instructive: three return-on-ad-spend metrics claimed they could be broken down by product, and advertising spend has no product dimension. The request would have passed every check and divided product-level revenue by spend spread across products — computable, plausible, wrong.

**So the capability being unlocked is not "answers questions about Amazon data."** It is **answers you can take into a meeting and defend**, including the answer "that cannot be known, and here is why."

---

## 6. Open product questions

**These questions are still open.** They cannot be derived from the code, and answering them with guesses would make the rest of this document less reliable. Where a question has a registry entry, the heading links to it.

### What the current pain costs

No cost estimate exists, and a dollar figure may not be the right measure. A simpler proxy is two questions to a customer: **how many separate reports do you pull, and how long does reconciling them take before you can answer anything?**

With spreadsheets and disparate reports as the baseline (§2), that count is the pain. It also gives a before-and-after that can be shown rather than argued.

### Which answerable questions matter most

The platform can compute new-to-brand rate by campaign, but how much customers value that answer is not yet known. [answerable-questions](answerable-questions.md) lists roughly twenty answerable question shapes without a ranking, so build order follows technical dependency rather than demand.

### The outcome list, [Q-11](decision-coverage-registry.md#question-register-q-nn)

Seven outcomes are designed ([business outcome measurement](concepts/business-outcome-measurement.md) §1). The complete list Q-11 asks for is still open.

### How the product should behave without a paid subscription, [Q-07](decision-coverage-registry.md#question-register-q-nn)

The AMC Paid Features subscription belongs to whoever deploys this template, so it is a customer prerequisite; the tiers are in [answerable-questions](answerable-questions.md) §2a.

What remains open is a product question: on a deployment without the subscription, should the three affected capabilities be **silently absent**, or **shown as available with the subscription**? The second is more honest, and it's the owner-approved default for the business outcomes (open decision 1, 2026-10-06). The catalog now tells "impossible for anyone" apart from "not licensed here": a paid metric with no delivery in its last three periods refuses as `not_observed_in_deployment`, never as unsupported ([ADR-107](decisions/adr-107-derive-paid-feature-availability-from-delivery-evidence.md); [business outcome measurement](concepts/business-outcome-measurement.md)).

Three paid-feature capabilities are affected. Brand and variation hierarchy are not among them, because Vendor Central supplies those attributes without a subscription.

### How answers get consumed

Chat only, or also scheduled reports, exports, alerts? [Q-14](decision-coverage-registry.md#question-register-q-nn) settled the *technology* — React, responsive, our own components — but not the interaction model. Alerting in particular would change the data layer, because it implies evaluating conditions on a schedule rather than only on request.

---

## 7. Input that would close them

If you deploy or evaluate this template, four pieces of input would resolve §6. [CONTRIBUTING](../CONTRIBUTING.md) explains how to send them.

1. **Which question shapes** in [answerable-questions](answerable-questions.md) §3 matter most to a brand manager.
2. **Any outcome you need that is missing from that list.** If it sits in §6 of that document, the never-answerable set, it is a limit to know before relying on the product.
3. **How many reports** you reconcile by hand, and how long it takes.
4. **Whether a capability your deployment cannot access** should be hidden or shown as needing the subscription.

As each is answered it moves into §1–§5, and [Q-11](decision-coverage-registry.md#question-register-q-nn) closes from both directions.
