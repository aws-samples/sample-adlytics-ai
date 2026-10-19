# ADR-088: Model budget, pacing and pool state as platform-neutral snapshots
Date: 2026-10-07
Status: Proposed. Not accepted or implemented. The response-curve and forecasting design was approved on the owner's behalf on 2026-10-07, under authority the owner delegated; the owner hasn't reviewed the final design yet. It implements the `snap_` kind that [ADR-003](adr-003-canonical-model-decomposition.md) already decides; it doesn't re-decide it. The design is summarised in [Response curves and budget simulation](../concepts/response-curves-and-budget-simulation.md).
## Context
The simulator turns a proposed budget into expected spend with the platform's own rules. That needs budget, pacing, portfolio and capping state per campaign and day, which the canonical model doesn't hold today. AMC carries spend and outcomes, but not the sponsored budget amount; the Amazon Ads API does.
Budgets aren't facts that can be summed. A budget is a state that holds from when it was seen. Amazon's rules on top of it (the monthly average, a day's allowed overspend, portfolio sharing) can change and differ by ad product. And two rules in the repository constrain the shape:
- one contract is one source dataset, one grain and one target table ([ADR-022](adr-022-contract-decomposition-by-dataset.md));
- canonical publication compiles one source publication one to one ([ADR-044](adr-044-materialize-source-aligned-data-and-centralize-canonical-publication.md)).
## Decision
**Budget, pacing and pool state are platform-neutral `snap_` tables. The rules that turn them into allowances, capped days and bridge weights are versioned domain functions, never canonical derivations.**
1. **The `snap_` kind in the validator.** The contract validator accepts `snap_` targets beside `fct_` and `dim_`, with their provenance projection and the rule that a snapshot is never summed over its observation time. Snapshot source packs use the existing `fact` role and `snapshot` time class.
2. **New state tables**, each in canonical, platform-neutral kinds:
   - `snap_campaign_budget_state`: campaign, flight or ad-group scope; daily, lifetime, flight or monthly type;
   - `snap_budget_pool_state`;
   - `snap_budget_settings`;
   - `snap_budget_usage`;
   - `snap_budget_rule_association`;
   - `snap_campaign_delivery_settings`;
   - `snap_campaign_fee_line`;
   - `snap_product_availability`;
   - `snap_promotion_status`;
   - `snap_platform_forecast_vintage`;
   - `snap_platform_forecast_curve_point`.
3. **New dimensions and facts:**
   - `dim_ad_account`, `dim_ad_group`, `dim_campaign_product_membership`, `dim_promotion_product` and `dim_retail_event`;
   - `fct_campaign_pacing`, `fct_campaign_change_event`, `fct_campaign_hourly_cost`, `fct_ad_exposure_hourly`, `fct_ad_exposure_by_product`, `fct_attributed_conversion_by_traffic_date` and the optional `fct_lift_test_result`;
   - `_withheld` companions for the two new AMC facts.

   The hour is a grain column on a `DAY`-bucket `daily_additive` fact, not a new time grain.
4. **Every canonical table stays one to one with its source grain.** These cross-dataset steps are named, versioned domain functions with property tests, run where inputs are built, not in canonical publication:
   - ad → ad group → campaign membership;
   - promotion and coupon days;
   - budget-state precedence;
   - effective allowance, portfolio sharing allowance, capped flags and regimes;
   - bridge weights.
5. **Configured membership is the only curated bridge fact.** The walk-back and served fallback are bridge derivations.
6. **Levels, not ratios.** The one in-row derivation is `product_of`, the product of two fields of the same record (for example usage percent × budget). Ratios a source returns stay source-aligned.
7. **Every contract states its time class, observation basis and a literal `measurement_basis`.** Amazon forecasts and recommendations are `modelled`.
8. **No fifth null meaning.** State before capture is an absent row or a closed `unknown` value, under the four existing null meanings.
## Consequences
- About 23 new canonical tables and 2 `_withheld` companions. The ingestion dataset lists and publication-authority keys are generated from the contracts rather than kept by hand (owner, 2026-10-06).
- Changing a pacing rule is a new domain-function version, recorded in each input manifest. It needs no reload and no contract change.
- Several contracts write one canonical table (for example, five budget sources feed `snap_campaign_budget_state`). Their writes take turns on that table's load lane.
- [`docs/canonical-model.md`](../canonical-model.md#remaining-facts--deferred-outside-semantic-v2) lists these tables now as deferred, with grain and domain, and the [Data Layer bus matrix](../capabilities/data-layer/architecture.md#canonical-bus-matrix) has a row for each new fact. A table becomes a live canonical fact, with field-level columns, when its first contract ships.
## Alternatives considered
| Alternative | Why not, or why |
|---|---|
| Store budgets as `fct_` rows | A budget summed across days is silently wrong |
| A derived canonical publisher that joins several sources (allowance, capped flags, membership) | It would change ADR-044's implementation and the publication graph, for rules that are policy and change on their own schedule |
| **Snapshot tables for state, and versioned domain functions for the rules, run when inputs are built** | **Chosen** |
## Open questions
- Whether Ads API v1 daily budget columns hold the value as of each day is [unverified] until a planned probe of a recorded report checks it. Until then, budget history before capture comes from 90 days of Change history (Sponsored Products and Sponsored Brands only).
- Open items shared with other decisions, such as sharing under the average-daily-budget opt-out, are listed in [Response curves and budget simulation §6](../concepts/response-curves-and-budget-simulation.md#6-open-items).
## Evidence
- ADR-003 decides the `snap_` prefix for snapshot-grain facts [verified 2026-10-06, repository file `docs/decisions/adr-003-canonical-model-decomposition.md`, line 133]. `contract.py` accepts only `fct_` and `dim_` targets today [verified 2026-10-06, repository file `src/domain/data_contracts/contract.py`].
- Canonical publication reads one source publication and refuses more than one [verified 2026-10-06, repository file `src/domain/semantics/canonical_compiler.py`].
- AMC `sponsored_ads_traffic` has `campaign_budget_type` but no budget amount [verified 2026-10-05, AMC data source page].
- Change history returns up to 90 days of budget, in-budget, status, date and bid changes, for Sponsored Products and Sponsored Brands only [verified 2026-10-05, Amazon Ads Change history OpenAPI specification].
## Review trigger
A platform whose budget model can't be expressed in these kinds, or a need to publish a derived table that joins sources.
