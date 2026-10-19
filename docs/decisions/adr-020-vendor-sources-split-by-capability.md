# ADR-020: Vendor Central sources are split by capability, not chosen between

Date: 2026-08-19
Status: Accepted

## Context

Vendor Central retail analytics is reachable through two different doors, and [Q-05](../decision-coverage-registry.md#question-register-q-nn) has been open on which to use.

| | Reports API | Data Kiosk |
|---|---|---|
| Interface | REST, one report per call | GraphQL |
| Output | JSON, **two grains per document** | JSONL, flatter |
| **`parentAsin`** | **absent** | **present** |
| Group-by | fixed per report | `asin`, `parentAsin`, `brand`, `productGroup`, geo |
| Marketplaces | one per report | several per call |
| Rate pressure | **`createReport` 1 per minute** | fewer calls for the same coverage |
| Freshness | 48–72h | ~34h |
| Amazon's investment | — | every 2025–26 vendor-analytics release lands here |

All [verified] in [sources/vendor-central](../sources/vendor-central.md).

**The decision has been treated as a choice, and framing it that way produced a deadlock.** Each option loses something that matters.

Choosing the **Reports API** leaves the parent-versus-child ASIN question unresolved. Amazon's docs never state whether vendor report ASINs are parent or child [unverified, and named the single biggest identifier gap]. If vendor carries parents and AMC carries children, every join at product grain silently aggregates the wrong things — both sides look like valid 10-character ASINs, so nothing errors and no suppression flag labels it. It is the one correctness risk in the data layer with **no** structural mitigation.

Choosing **Data Kiosk** discards verified work. Our `[verified]` vendor field lists come from the Reports API JSON Schemas in `amzn/selling-partner-api-models`; Data Kiosk's shape is different, so switching means redoing that research at the depth we just applied to AMC's 1024 columns. Our knowledge of it is one page deep.

**And there is a constraint on both that is easy to forget: this repository does not run extraction.** The producer that operates extraction (outside this repository) pulls from Amazon and lands files in S3; this repository's scope starts at the S3 object ([source-acquisition](../capabilities/source-acquisition/README.md) §1). REST versus GraphQL is the producer's implementation burden, so this belongs in the raw-zone renegotiation already open as [F-01](../decision-coverage-registry.md#finding-register-f-nn) rather than in a decision handed to the producer.

**The reframe.** "Both" has two meanings and only one is viable. Loading the *same dataset* from both sources means two versions of the same revenue that will disagree — different freshness windows and different view semantics (`distributorView` versus `sourcingView`/`manufacturingView`) — which imports the non-reconciliation problem [ADR-002](adr-002-canonical-data-model.md) exists to avoid. Taking *different datasets* from each does not.

## Decision

**Sources are split by what each uniquely supplies.**

| Source | Supplies | Feeds |
|---|---|---|
| Reports API | sales and traffic facts | `fct_retail_sales`, `fct_product_page_views` |
| Data Kiosk | one query grouped by `asin` and `parentAsin` | the `dim_product` hierarchy |

> **Amended 2026-08-19 — the Data Kiosk query also carries product attributes, and one of them closes a gap this ADR thought was permanent.** Its group-by set includes `brand`, `brandCode` and `productTitle` alongside `asin` and `parentAsin` [verified, [sources/vendor-central](../sources/vendor-central.md) §10].
>
> **`dim_product.brand` therefore needs neither AMC's paid ARP feature nor an external catalogue.** Both this ADR and [sources/amc](../sources/amc.md) §9 sent brand to ARP or a catalogue and called it a procurement decision. It is not one. Brand membership was otherwise only *implied* by AMC's `brand_halo_*` metric family, which tells you a halo product shares the brand without ever naming it — which is why "how did brand X perform" sat in the never-answerable bucket.
>
> The narrow-ask principle below is unchanged: this is still **one** query, and these are attributes of the entity it already returns rather than a second dataset. Writing the contract is what surfaced it — see `contracts/vendor-central/vendor-product-hierarchy.yaml`.

**Never both for the same dataset.** One dataset has one source. A contract that would write a fact another contract already writes from a different source is rejected.

**The Data Kiosk ask is deliberately narrow.** One query returning child-to-parent pairs, not a second reporting pipeline. That keeps the producer's additional burden to a single GraphQL query rather than a parallel implementation, and it keeps our verified Reports API schema work valid.

**Q-05 is closed by this.** The question was which to use; the answer is that it was the wrong question.

## Consequences

**The parent-versus-child ambiguity is resolved rather than mitigated.** Data Kiosk states the hierarchy explicitly, so there is nothing left to infer. This removes the only unmitigated correctness risk in the data layer, and it removes the third-source dependency ([sources/amc](../sources/amc.md) §9 pointed at ARP, a paid feature, or the Catalog Items API).

**Two API surfaces exist upstream, and that is a real cost** carried by the producer that operates extraction (outside this repository). It is justified by what the second one buys and bounded by how small it is.

**`dim_product` becomes buildable in V1** rather than deferred. Sequencing it after the first facts load is still right — the hierarchy only matters once something joins across sources — but it is no longer blocked.

**Swapping remains cheap, which is the point.** If Data Kiosk later wins for the facts too, that is `source_format: jsonl` plus a new field-mapping table producing the *same* `target_table`. `SourceFormat.JSONL` already exists for it. The canonical model, the facts, the tools and the API do not move. A contract is the adapter, and this is that property being spent rather than admired.

**One residual stays open.** Whether Reports API ASINs are parent or child is still undocumented. This decision means we no longer *depend* on the answer, because the hierarchy comes from a source that states it — but a mismatch between the two vendor surfaces would surface as unresolvable ASINs in `dim_product`, which is a loud failure rather than a silent one. That is the trade: an unknown becomes a validation error instead of a wrong number.

## Alternatives rejected

**Reports API only.** The status quo assumption, and the cheapest. Rejected because it leaves the parent/child ambiguity unresolved, and that risk is not reducible by anything we build — no threshold flag, no coverage metadata and no narration labels a join that silently grouped the wrong products.

**Data Kiosk only.** The strongest long-term case: it is where Amazon is investing, it has better freshness, it costs fewer calls, and it carries `parentAsin` natively. Rejected as a V1 move because it discards verified schema work for a surface we have read one page of, and because "documented and never observed" is exactly how the CSV-versus-JSON error in the original brief survived to become [F-01](../decision-coverage-registry.md#finding-register-f-nn). Revisit once the vendor surface has live validation.

**Both sources for the same dataset, reconciled downstream.** Rejected outright. Two measurement paths for one fact is the thing [ADR-002](adr-002-canonical-data-model.md) refuses, and adopting it voluntarily to hedge a sourcing decision would be worse than either option alone.

**Defer the hierarchy to V1.1 and ship single-source only.** The previous plan, and still a sound fallback. Superseded because the Data Kiosk ask turned out to be one query rather than an integration, which changes the cost enough to change the answer.

## Review trigger

Reopen if the producer that operates extraction (outside this repository) cannot or will not build against Data Kiosk. In that case the fallback is the V1.1 plan — single-source answers only, no cross-source product join — and the parent/child question returns to being a blocker rather than a resolved one.
