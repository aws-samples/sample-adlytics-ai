# ADR-052: Separate ecosystem, platform, channel, and ad product

Date: 2026-08-24
Status: Accepted

> **Proposed amendment, 2026-10-06.** [ADR-106](adr-106-keep-outcome-metrics-inside-one-network-by-type.md) (Proposed) would give the declared mixed-basis ratio a typed form: a per-metric `ecosystem_aggregation` rule with platforms derived per operand. This decision stays in force, unchanged, until ADR-106 is accepted.

## Context

[ADR-002](adr-002-canonical-data-model.md) and [ADR-003](adr-003-canonical-model-decomposition.md)
decide that source-native vocabulary maps into conformed dimensions and that advertising channel is
a dimension rather than a source-owned domain. They do not decide how a broad customer phrase such
as “Amazon performance” relates to Amazon Ads, Amazon retail, Sponsored Products, DSP, AMC, or
Vendor Central.

The current vocabulary conflates those roles. Canonical documentation describes channel keys and a
platform attribute; active AMC contracts currently place `ad_product_type` into both channel and ad
product for sponsored traffic and stamp both as DSP for DSP traffic; the prototype groups all
sponsored products under a presentation-only sponsored-ad label. Vendor retail facts correctly carry
no advertising channel, while one stale crosswalk says to stamp `amazon` as their channel.

That ambiguity becomes structural when a second ecosystem or advertising platform arrives. “Amazon
versus Google” is an ecosystem comparison. “Amazon Ads versus Google Ads” is a platform comparison.
“Sponsored Products versus DSP” is a channel comparison. “AMC versus Vendor Central” is a source or
reconciliation question. “Attributed versus retail actual” is a measurement-basis question. Treating
all five as channel produces groupings that are executable, plausible, and semantically wrong.

## Decision

**Ecosystem, platform, advertising channel, channel group, and ad product are separate governed axes.
Source system, business process, measurement basis, marketplace, and commerce scope remain
orthogonal and are never inferred from one of those axes.**

The conformed hierarchy is:

| Axis | Meaning | Example role |
|---|---|---|
| `ecosystem` | broad customer-facing business environment | Amazon or Google |
| `platform` | service on which a business process occurs | Amazon Ads, Amazon Retail, Google Ads |
| `channel_group` | cross-platform media family | search, display, video |
| `channel` | governed advertising delivery channel | Sponsored Products or Amazon DSP |
| `ad_product` | source buying or serving product | a platform-specific advertising product |

`ecosystem` and `platform` become first-class semantic axes available to the metric compatibility
model. A metric may group or filter on either only when its typed axis policy permits it. Advertising
channel is available only on advertising facts. Retail facts have an ecosystem and retail platform
but no advertising channel.

Source system and dataset remain lineage. AMC can produce both ad-attributed advertising facts and a
retail-actual purchase measurement; Vendor Central can produce retail actuals. Therefore neither
`source_system=amc` nor `source_system=vendor_central` determines platform, channel, or measurement
basis without a governed source mapping.

One versioned taxonomy maps source-qualified labels to canonical axes. Source labels never become
canonical keys by string copying. `ad_product` may map to a channel, but the relationship is explicit
and effective-dated rather than inferred because two columns currently happen to contain the same
string.

“Amazon performance” resolves to `ecosystem=amazon` and produces separately labelled business
process and measurement-basis sections. It does not sum advertising-attributed revenue and retail
actuals. “Amazon Ads versus Google Ads” resolves to a platform comparison and is answerable only for
a metric whose scope, currency, time, attribution, fee, and aggregation semantics pass the existing
compatibility rules. An unloaded platform produces a capability refusal, not an empty or fabricated
comparison.

This decision extends channel-as-dimension; it does not create platform-specific fact schemas and
does not require a consolidated cross-channel fact. Source onboarding still maps natural-grain
facts into conformed axes.

## Consequences

Customer language can distinguish ecosystem, platform, media channel, buying product, source, and
measurement basis. “Amazon,” “Amazon Ads,” “DSP,” “AMC,” and “retail actual” no longer compete for
one overloaded channel field.

The metric dictionary and planner must expose platform and ecosystem only after the physical facts
or governed mappings can supply them. The typed `SemanticAxis.PLATFORM` seam is not evidence that a
queryable column already exists.

Current contract, canonical, and presentation channel values require one migration to the accepted
vocabulary. The physical half follows [ADR-029](adr-029-iceberg-for-the-curated-zone.md): preserve
existing IDs, append migration-nullable axis columns, deploy DataV2 and the updated ingestion writer,
reprocess retained exact evidence and validate new publications, then deploy Agent/API readers.
Until that sequence completes, natural-language resolution cannot claim a stable deployed cross-layer
channel identity.

A future Google Ads connector supplies new source mappings, platforms, channels, ad products, metric
compatibility, and evidence. It does not require new tool families or a second canonical model.

Retail actuals remain outside advertising-channel grouping. Platform-level scorecards may present
retail and advertising together only as separate compatible measurements or an explicitly declared
mixed-basis ratio.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Model AMC and Vendor Central as one Amazon channel | Conflates source lineage, retail platform, advertising channel, and measurement basis |
| Use source system as platform | AMC emits more than one business process and basis; source is provenance rather than customer-facing analytical identity |
| Keep platform only as a channel attribute | Cannot represent Amazon Retail without inventing an advertising channel and cannot group platform safely where channel is unavailable |
| Make ad product and channel the same key | Works only while one source vocabulary happens to align and prevents independent taxonomy evolution |
| Add one schema per platform | Reverses ADR-003 and makes every new source a structural fork |

## Review trigger

Revisit when a supported ecosystem contains no meaningful platform boundary, or when a verified
source proves that two axes are semantically identical for every supported metric and business
process rather than merely carrying equal strings today.
