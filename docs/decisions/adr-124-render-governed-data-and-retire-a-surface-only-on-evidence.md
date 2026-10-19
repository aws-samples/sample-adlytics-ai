# ADR-124: Render governed data, and retire a surface only on evidence
Date: 2026-10-07
Status: Proposed. Records the owner's principle of 2026-10-07 as an information architecture rule. The
re-audit outcomes in decision 2 were accepted as designed the same day by the workflow orchestrator,
under the owner's delegated overnight authorization. Not implemented. The re-audit is in
[Information architecture](../ui/information-architecture/README.md#5-render-more-re-audit).

## Context

The first information architecture rounds retired several surfaces because no requirement asked for
them: channel delivery, model sensitivity and the product drill-in. The owner then said: "if we have
the data lets render it the more the better we can continue to iterate, its open source project"
(2026-10-07). Some retired surfaces had governed data. Amazon Vendor Central metrics are valid at
`product` grain [repo: `contracts/shared/metric-dictionary.yaml:505-718`]. Clicks, CTR, CPC and CPM
are valid at `channel` grain (`:146-160`, `:745-817`), and the deduplicated ROAS is valid per channel
under each registered model (`:1028-1046`). Cost of goods shipped renders on dev today beside the
margin proxy [repo: `frontend/components/RetailHealthView.tsx:41`].

[ADR-117](adr-117-keep-pages-and-apis-only-on-merit-and-retire-a-replaced-surface-with-its-replacement.md)
judges every existing surface on merit. This decision sets what counts as merit for a surface that has
governed data.

## Decision

**Where governed data exists, it renders. A surface retires only when it has no data, no governed
source, or repeats a fact that already has a home. Data that's coming gets a typed absence that turns
on by itself when the data lands.**

1. **The rule doesn't relax any other rule.** One window per page; the basis on every ROI; no attributed
   total across channels or networks; "Not available" with a plain reason, never zero; server text only;
   insight deep dives on every tab with insight contracts.
2. **Re-audit outcomes.**
   - *Render:* Retail health › product detail
     ([ADR-125](adr-125-serve-retail-product-detail-as-a-baked-child-of-retail-health.md)); delivery per
     channel, with the Amazon DSP viewable rate, in Campaigns › By channel
     ([ADR-127](adr-127-serve-the-campaigns-channel-filter-from-render-only-siblings-of-one-bake.md));
     the ranking under each attribution model on the Scorecard
     ([ADR-126](adr-126-render-the-scorecard-ranking-under-each-attribution-model.md)); cost of goods
     shipped, average selling price and revenue per glance view on Retail health and product detail.
   - *Retire:* Media Efficiency's audience slots, which are hard-coded unavailable
     [repo: `src/domain/presentation/media_efficiency.py:241-243`]; Marketplace Movement, Evidence
     Health and the v1 decision overview, whose facts have homes; the request-time Media Efficiency and
     Channel Performance APIs, whose facts now render in hub homes.
   - *Point to the home:* new-to-brand purchases per product, which Customers › New customers ›
     Gateway products owns.
   - *Typed absence:* sponsored viewability and return reasons, which no governed source supplies.
   - *Paid feature:* Amazon Marketing Cloud (AMC) retail purchases, shown as "Available with the paid
     feature".
3. **Replacements leave no second path**, under ADR-117 decision 2.
4. **Owner-owned requirements aren't changed here.** The Scorecard's model selector, a business
   outcomes requirement, stays. Every model credits each deduplicated purchase once, so the network
   headline is the same under each
   [repo: `contracts/amc/query-packs/amc-deduplicated-temporal-attribution.sql:110-150`], and the
   selector changes no figure today. Its note says so. Whether to retire it is logged for the owner of
   that requirement.

## Consequences

- More surfaces to build and test: about 150 more stored child bodies per publication for product
  detail, one more AMC product query per preset, and one deduplicated ranking query per Scorecard
  preset. Delivery per channel adds no measurement batch.
- Each new surface carries an absence state from the coverage block
  ([ADR-119](adr-119-serve-window-coverage-as-one-server-written-evidence-status-block.md)), so data
  that's coming is a designed state, not an empty panel.
- A future surface is argued from its data, not from whether a requirement names it.

## Alternatives considered

- **Render only what a requirement names.** The rule the owner replaced. It left governed data unseen.
- **Render everything, including duplicates.** Two homes for one fact, with different bases, would
  contradict each other.

## Review trigger

Reopen when a rendered surface's cost outweighs its use, measured on a deployment, or the owner changes
the principle.
