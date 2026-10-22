# Glossary

| | |
|---|---|
| **Status** | Living. Grows when a term appears in a second document |
| **Last updated** | 2026-10-22 |
| **Scope** | Acronyms and terms used across these documents, with a pointer to whoever owns each |
| **Related** | [conventions](conventions.md) §9 · [understanding-the-data-model](understanding-the-data-model.md) · [business-context](business-context.md) |

**This file defines vocabulary and owns no facts.** A one-line gloss so an acronym is not a search, then a pointer to the document that owns the thing. When a definition here feels like it needs a second sentence, that sentence belongs in the owning doc. [conventions](conventions.md) §9 asks of every documentation change: is every acronym either obvious or here.

Terms specific to one document are defined there and deliberately absent from this list — a term used once has one home already.

## Amazon and advertising

| Term | Gloss | Owner |
|---|---|---|
| **1P** | First-party: Amazon buys from the brand and resells. The alternative, 3P, sells through Amazon as a marketplace seller. This platform is 1P only | [business-context](business-context.md) |
| **AMC** | Amazon Marketing Cloud. A privacy-safe clean room holding event-level advertising data, queried with SQL whose output is aggregated before we see it | [sources/amc](sources/amc.md) |
| **AOV** | Average order value. Revenue divided by orders | [canonical-model](canonical-model.md) |
| **ARP** | Amazon Retail Purchases. An AMC paid-feature table carrying 60 months of retail purchase history, and the documented reconciliation anchor between AMC and Vendor Central | [sources/amc](sources/amc.md) |
| **ASIN** | Amazon Standard Identification Number. Amazon's product identifier, marketplace-scoped, and the natural key `dim_product` is built on | [canonical-model](canonical-model.md) §1, [ADR-023](decisions/adr-023-product-natural-key.md) |
| **brand halo** | Sales of a brand's *other* products credited to an ad for one of them. Excluded from the default ROAS | [ADR-017](decisions/adr-017-roas-definition.md) |
| **CVR** | Conversion rate. Conversions over some upstream count, and which count matters | [canonical-model](canonical-model.md) |
| **Data Kiosk** | An SP-API GraphQL interface. Used for the ASIN hierarchy only, never for facts | [ADR-020](decisions/adr-020-vendor-sources-split-by-capability.md) |
| **DSP** | Amazon Demand-Side Platform. Programmatic display and video buying. Its impressions carry no ASIN, which is why product-grain ad spend does not exist | [canonical-model](canonical-model.md) §5 |
| **glance view** | A page view of a product detail page, as Vendor Central counts it | [sources/vendor-central](sources/vendor-central.md) |
| **GTIN** | Global Trade Item Number. A retailer-independent product identifier, and the missing piece for cross-retailer product identity | [canonical-model](canonical-model.md) §3 |
| **NTB** | New-to-brand. A purchase by a customer who has not bought from the brand in a stated lookback | [canonical-model](canonical-model.md) §5 |
| **Paid Features** | AMC datasets requiring a separate subscription. A customer prerequisite, not a cost of ours | [answerable-questions](answerable-questions.md) §2a, [Q-07](decision-coverage-registry.md#question-register-q-nn) |
| **ROAS** | Return on ad spend. Ambiguous by nature — attributed or blended, halo-inclusive or not, which window, whether fees count | [ADR-017](decisions/adr-017-roas-definition.md) |
| **SP-API** | Selling Partner API. Amazon's API surface for vendor and seller data, including the Reports API | [sources/vendor-central](sources/vendor-central.md) |
| **suppression** | AMC withholding a row that would identify too few users. Destructive by default: without the discriminator column set, rows are **deleted** rather than flagged | [sources/amc](sources/amc.md) §5, [provenance-chain](concepts/provenance-chain.md) |
| **Vendor Central** | Amazon's 1P vendor portal. Source of retail sales and traffic facts | [sources/vendor-central](sources/vendor-central.md) |

## The data model

| Term | Gloss | Owner |
|---|---|---|
| **additive** | A measure that can be summed across any dimension and still be right. Impressions are; a rate and a distinct count are not | [understanding-the-data-model](understanding-the-data-model.md) §4 |
| **bus matrix** | A grid of facts against conformed dimensions, showing which questions can cross which sources | [data-layer](capabilities/data-layer/architecture.md) §4 |
| **canonical model** | The conformed analytical layer tools read. Dimensions and metric semantics are conformed; facts are not | [canonical-model](canonical-model.md), [ADR-002](decisions/adr-002-canonical-data-model.md) |
| **contract** | A declarative YAML file mapping one source dataset into the canonical model. Eleven of them | [data-layer](capabilities/data-layer/README.md) |
| **delivery** | One dataset at one grain for one marketplace over one window, as one set of objects plus a manifest. The unit the load path decides about | [provenance-chain](concepts/provenance-chain.md) |
| **landing contract** | Where a delivery goes in the raw zone and what says it is complete. The agreement with whoever runs extraction | [ADR-030](decisions/adr-030-the-raw-zone-landing-contract.md) |
| **load path** | The sequence that turns a manifest-bound delivery into immutable source evidence, a validated candidate, and a fenced current publication | [load-path](capabilities/load-path/README.md) |
| **manifest** | The authoritative statement of what arrived, written **last** so its arrival is the trigger. It binds the exact immutable delivery. | [Source acquisition](capabilities/source-acquisition/README.md) |
| **rejected landing** | A delivery we cannot identify at all, so there is nothing to quarantine. Distinct from a skip and from a quarantine | [ADR-030](decisions/adr-030-the-raw-zone-landing-contract.md) |
| **control total** | A source-reported aggregate compared against the detail rows that should sum to it. Exact equality; a mismatch quarantines | [canonical-model](canonical-model.md) §4 |
| **coverage** | What period a fact actually holds, and how current it is. Partial overlap is deliberately not coverage | [Publication State](capabilities/publication-state/README.md) |
| **DQ / DQDL** | Data quality, and Glue's Data Quality Definition Language. Rulesets generated per table from the contracts | [load-path](capabilities/load-path/architecture.md) §5 |
| **drill-across** | Answering with two facts by aggregating each to a shared grain and joining the results, never by joining rows. The only honest way to compute a ratio spanning two facts | [understanding-the-data-model](understanding-the-data-model.md) §5 |
| **grain** | What makes one row of a fact distinct. Stated, never inferred, and a query that sums across a grain column is claiming that column is addition | [ADR-024](decisions/adr-024-plans-account-for-every-grain-column.md) |
| **Iceberg** | Apache Iceberg. The curated table format — schema evolution by field id, snapshot isolation, copy-on-write updates | [data-layer](capabilities/data-layer/README.md) |
| **measurement basis** | Whether a figure is ad-attributed, retail-actual or modelled. Three bases that do not reconcile with each other by design | [canonical-model](canonical-model.md) §3 |
| **metric dictionary** | `contracts/shared/metric-dictionary.yaml`. The one declaration of what a metric means and which grains it is valid at | [canonical-model](canonical-model.md) §5 |
| **quarantine** | Reject a partition, alarm, and leave the last good partition live. Never drop a row, never coerce a value | [load-path](capabilities/load-path/architecture.md) §5 |
| **reporting lens** | A grain column selecting a *view* of one population rather than a different population — `distributor_view`, `selling_program`. Defaultable, and every default is stated | [ADR-027](decisions/adr-027-a-tool-may-default-a-lens-never-a-population.md) |
| **restatement** | A source rewriting figures it already published; selected coverage changes only when the replacement publication becomes `CURRENT` | [sources/vendor-central](sources/vendor-central.md) §7 · [Publication State](capabilities/publication-state/architecture.md) §5 |
| **SCD2** | Slowly changing dimension, type 2. A row per version of a thing, so a question about last quarter gets last quarter's attributes | [slowly-changing-dimensions](concepts/slowly-changing-dimensions.md) |
| **star schema** | A fact in the middle, dimensions around it, joined by keys | [understanding-the-data-model](understanding-the-data-model.md) §3 |
| **watermark** | A source-provided statement of when a delivered window last changed | [sources/vendor-central](sources/vendor-central.md) §7 · [provenance-chain](concepts/provenance-chain.md) §2.2 |

## The platform

| Term | Gloss | Owner |
|---|---|---|
| **ADOT** | AWS Distro for OpenTelemetry. The tracing SDK, chosen over the X-Ray SDK | [ADR-009](decisions/adr-009-observability-instrumentation.md) |
| **ADR** | Architecture Decision Record. Owns *why*, and the history of why | [conventions](conventions.md) §7 |
| **AgentCore** | Bedrock AgentCore. Runtime, Memory, Gateway and Policy — the agent layer | [ADR-006](decisions/adr-006-agentcore-agentic-layer.md) |
| **Cedar** | The policy language a policy engine evaluates. Denies by default | [ADR-042](decisions/adr-042-guardrails-at-the-gateway-do-not-reach-a-lambda-target.md) |
| **arc42** | The template deciding what documents exist and what sections they have | [conventions](conventions.md) §2 |
| **BFF** | Backend for frontend. The pattern holding tokens server-side so the browser only ever carries an `HttpOnly` cookie | [ADR-007](decisions/adr-007-auth-cookie-bff.md) |
| **C4** | The model deciding what a diagram shows and at which zoom level | [conventions](conventions.md) §2 |
| **CDK** | AWS Cloud Development Kit. The infrastructure is CDK v2 in TypeScript | [repo-structure](repo-structure.md) §3 |
| **EMF** | Embedded Metric Format. How a log line becomes a CloudWatch metric with no extra plumbing | [observability](concepts/observability.md) |
| **Gateway** | AgentCore Gateway. Where tools are exposed as MCP targets and a tool call is authorized | [Conversational Runtime](capabilities/conversational-runtime/README.md) |
| **guardrail** | A managed content filter. Applied to a question before the Harness runs, never to a tool result or the streamed answer | [Conversational Runtime](capabilities/conversational-runtime/architecture.md) §4 |
| **hexagonal** | The architecture: imports point inward only, and the domain imports nothing outside itself | [repo-structure](repo-structure.md) §2 |
| **MCP** | Model Context Protocol. How a tool is described to and invoked by an agent | [Conversational Runtime](capabilities/conversational-runtime/README.md) |
| **OAC** | Origin Access Control. How CloudFront reaches a private S3 bucket | [ADR-008](decisions/adr-008-frontend-hosting.md) |
| **policy engine** | The AgentCore resource that evaluates Cedar for a gateway | [ADR-042](decisions/adr-042-guardrails-at-the-gateway-do-not-reach-a-lambda-target.md) |
| **OIDC** | OpenID Connect. The login redirect flow against Cognito | [authentication](concepts/authentication.md) |
| **PITR** | Point-in-time recovery for DynamoDB state | [Conversational Runtime](capabilities/conversational-runtime/low-level-design.md) §6 |
| **port / adapter** | A port is an interface stated in domain terms; an adapter is the thing that talks to AWS. A port never carries an SDK type | [repo-structure](repo-structure.md) §2 |
| **`ShapedResponse`** | The envelope every tool returns, and the only thing it returns. Forces coverage, basis and suppression to reach the user | [Conversational Runtime](capabilities/conversational-runtime/low-level-design.md) §6 |
| **baseline observation** | A target-free authority-backed measurement used to understand current behavior; it never carries an SLO result or health claim | [data-flow completion](concepts/data-flow-completion-and-service-levels.md) |
| **SLI** | Service-level indicator: one measured result under a declared flow clock, population, and evidence rule | [data-flow completion](concepts/data-flow-completion-and-service-levels.md) |
| **SLO** | Service-level objective: the versioned target against which eligible SLI results become met, missed, unknown, or not applicable | [data-flow completion](concepts/data-flow-completion-and-service-levels.md) |
| **SSE-S3** | Server-side encryption with S3-managed keys; customer-managed keys are a configuration option | [ADR-018](decisions/adr-018-encryption-at-rest.md) |
| **SSM** | Systems Manager Parameter Store. Carries every cross-stack value, because CloudFormation exports deadlock on update | [ADR-012](decisions/adr-012-stack-composition-and-config.md) |
| **steering** | Files under `.kiro/steering/` stating rules an agent must follow. Rules only; rationale lives in `docs/` | [meta-style-steering](../.kiro/steering/meta-style-steering.md) |
| **WCAG** | Web Content Accessibility Guidelines. The frontend colour tokens are tuned to WCAG AA contrast (`frontend/app/globals.css`); no formal conformance target is stated, and full validation requires manual testing with assistive technologies | [G-20](decision-coverage-registry.md#gap-register-g-nn) |

## Reading the docs

| Marker | Meaning |
|---|---|
| **[verified]** | The source was fetched and read, or the behaviour was observed against a real system |
| **[snippet]** | A search result only. Not confirmed against the source |
| **[unverified]** | Could not be confirmed. Stating this is not optional |
| **F-nn** | A conflict between the proposed design and current reality — [finding register](decision-coverage-registry.md#finding-register-f-nn) |
| **G-nn** | Something missing that nothing accounts for — [gap register](decision-coverage-registry.md#gap-register-g-nn) |
| **Q-nn** | An open decision — [question register](decision-coverage-registry.md#question-register-q-nn) |
| **D-nn** | A made decision, indexed in [architecture](architecture.md) §11 and owned by an ADR |

## Customer labels

The words customer pages show for a governed metric whose served label is jargon (UI refresh 2026-10-22, D5). Only the label changes; the metric, its value and its basis stay as served. [`frontend/lib/glossary.ts`](../frontend/lib/glossary.ts) owns the table; this list mirrors it for reviewers.

| Governed id | Customer label |
|---|---|
| `roas_deduplicated_incl_halo_14d` | Return on ad spend, each sale counted once |
| `roas_attributed_14d`, `portfolio_roas_attributed_14d` | Return on ad spend |
| `roas_attributed_incl_halo_14d` | Return incl. other products of your brand |
| `tacos` | Ad spend as a share of shipped sales |
| `cumulative_repeat_rate_account_ids` (Retention curve heading) | Bought again by then · signed-in shoppers |
| `glance_views` | Product page views |
| `revenue_per_glance_view` | Revenue per product page view |
| `amazon_margin_proxy` | Amazon's margin on your products (not yours) |
| `purchaser_rate_index_vs_sponsored_only` | Purchase rate compared with sponsored-only shoppers |
| Insight output classes `observed`, `perspective`, `hypothesis` | Fact, Reading, Idea to check |

Fixed page words that aren't metric labels change in the component that renders them (cross-cutting §2.1). The served string stays in the artifact.

| Page | Was | Customer words | Component |
|---|---|---|---|
| Reach | Amazon account IDs (overlap table) | Signed-in shoppers | `ReachAndFunnelView.tsx` |
| Reach | All ID types (served `all_ids_label`, decoder-pinned) | All shoppers incl. devices | `ReachAndFunnelView.tsx` |
| Reach | Platform return | Return on ad spend, all Amazon ads | `ReachAndFunnelView.tsx` |
| Reach | an unlabelled prior figure | {served prior period label}: {served prior}, or "Previous {grain}:" without a label | `ReachAndFunnelView.tsx` |
| Retention | Ad-touched repeat / All Amazon purchases | Repeat after an ad / Any repeat purchase | `RetentionView.tsx` |
| Retail health | Manufacturing view / Sourcing view | Ordered and shipped figures / Shipped figures only | `RetailHealthView.tsx` |
| Retail health | glance views (tile basis lines) | product page views | `RetailTiles.tsx` |
| Every insight card | Needs data / Retrying | Not enough data yet / Loading | `InsightCard.tsx` |
