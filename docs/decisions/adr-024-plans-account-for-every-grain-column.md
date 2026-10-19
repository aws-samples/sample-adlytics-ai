# ADR-024: A query plan must account for every column in the fact's grain

Date: 2026-08-19
Status: Accepted

## Context

Writing the query planner for Phase 2's target question — *"What was my shipped revenue by ASIN last week?"* — surfaced an arithmetic problem the metric dictionary does not describe.

`retail_revenue_shipped` declares `sum(fct_retail_sales.revenue_shipped)` at valid grains `[date, product, marketplace]`. The obvious plan is `SUM(revenue_shipped) ... GROUP BY amazon_asin` over a seven-day window. But [canonical-model](../canonical-model.md) §4 gives `fct_retail_sales` the grain *date × product × marketplace × distributor_view × selling_program*, and that plan aggregates two of those away.

**Summing across `distributor_view` double-counts.** A `MANUFACTURING` pull and a `SOURCING` pull of the same window describe the same units under two lenses, not two populations. The contract says so in its own comment — "only `MANUFACTURING` reconciles to AMC; `SOURCING` is a separate, non-reconcilable fact" [verified] — and [sources/vendor-central](../sources/vendor-central.md) §3 records the same thing about report options: without `reportSpecification` on every row, "a `MANUFACTURING` pull and a `SOURCING` pull are indistinguishable and will double-count". `selling_program` behaves the same way across `RETAIL`, `BUSINESS` and `FRESH`.

**Summing across `marketplace_key` does not double-count, but mixes currencies.** Marketplaces are genuinely distinct populations, so adding them is arithmetic. What makes it unsafe is different: `currency_code` travels with every amount ([ADR-021](adr-021-money-column-shape.md)), so a sum spanning marketplaces can span currencies. `Money` refuses to add across currencies, which is the right answer arriving far too late — the SQL has already collapsed them into one figure by the time any domain type sees it.

So two grain columns are unsafe to aggregate for two unrelated reasons, and a third — `date_key` — is entirely safe because the window bounds it.

**Nothing in a contract distinguishes the cases.** The natural candidate was `from_extraction_option`, and it does not work: that flag marks columns AMC *synthesises* from workflow options — the three suppression columns — and it is documented as such. The vendor option columns have real source paths (`reportSpecification.reportOptions.distributorView`), so they are not flagged and could not be. Nor is the distinction derivable from the column name, the type, or whether the column is a partition key.

The failure mode is the one this platform exists to prevent. Every surviving row is individually correct, the reported total is roughly double, no value is null, no rule is violated, and nothing about the answer looks wrong.

## Decision

**A plan must account for every column in the fact's declared grain. A column is accounted for when the plan groups by it, filters it, or — for `date_key` alone — bounds it with the query window. Any grain column left over refuses the plan, naming the columns.**

The rule is deliberately about *all* grain columns rather than about option columns specifically. Since the contract cannot say which ones are safe, a plan does not guess: it requires the caller to have made a decision about each, and records that decision in the plan. Grouping and filtering are both acceptable answers, and they differ in what the response says rather than in whether it is honest — grouping states the breakdown, filtering states the population.

`FactShape.grain_columns` is therefore required and must be non-empty. An optional or empty grain would turn the check into a no-op that permits exactly the double-count it exists to prevent.

The consequence for the target question is that it is under-specified as usually phrased. Answering it requires pinning `marketplace_key`, `distributor_view` and `selling_program`, and the tool must supply those and say so in the narrative.

## Consequences

**The double-count becomes structurally impossible rather than remembered.** No reviewer has to notice that a `GROUP BY` omitted an option column, and the rule holds for facts that do not exist yet — any future fact whose grain contains a lens column is covered the day it is added.

**Every retail question carries two Amazon-specific parameters.** This is the real cost. `distributor_view` and `selling_program` are vendor-reporting concepts that no brand manager thinks in, so the tool has to choose defaults and state them. `MANUFACTURING` is the defensible default because it is the only view that reconciles to AMC retail purchases and the only one under which `orderedRevenue` and `orderedUnits` are non-null; `selling_program` has no such argument and the choice is genuinely arbitrary until a real load shows which programmes a customer actually pulls.

**A refusal is raised rather than returned.** Consistent with `Refusal.to_error`: no query ran, so a response envelope would have to invent a measurement basis and a coverage window for figures that do not exist.

**The currency hazard is closed as a side effect.** Because `marketplace_key` must be accounted for, a monetary figure cannot silently span currencies. That was not the rule's purpose and it is the more valuable half.

**Ratios are refused for a separate reason and remain so.** A ratio aggregates each operand and joins the results, which single-fact planning cannot express. Refusing explicitly stops one operand's SQL from being served as a ratio.

## Alternatives rejected

| Rejected | Why |
|---|---|
| **Default `distributor_view` to `MANUFACTURING` silently** | The cheapest option and the most dangerous. A silent default means a `SOURCING`-only customer gets an empty answer with no explanation, and a customer pulling both gets a figure whose population nobody chose. The platform's whole argument is that a plausible wrong number is worse than a refusal |
| **Always group by every unaccounted grain column** | Never wrong arithmetically, and it answers a different question than the one asked. "Revenue by ASIN" would return a row per ASIN per view per programme per marketplace, and the caller would have to re-aggregate — which puts the double-count back one layer up, where nothing checks it |
| **Restrict the rule to columns identified as extraction options** | The shape we wanted, and the contract cannot supply it. `from_extraction_option` means something narrower and the vendor option columns are not flagged by it. Adding a new contract field to mark "lens" columns was possible but would be a declaration a contract author could forget, on exactly the columns where forgetting is invisible |
| **Check for the double-count in the DQ gate instead** | The gate validates loaded rows against a ruleset; it cannot see how a later query aggregates them. The rows are correct — the reading is wrong |
| **Detect at runtime whether more than one option set is present, and only refuse then** | Attractive, because a customer pulling only `MANUFACTURING` has no ambiguity. It needs the freshness registry to report which `report_options_hash` partitions exist, and it makes correctness depend on what happens to be loaded — so the same question is answerable one week and refused the next. Worth revisiting as a *relaxation* once the registry exists, but not as the rule |

## Review trigger

Revisit when the freshness registry can report which option sets a deployment actually holds. If a customer only ever pulls one, requiring them to pin it is friction without safety, and the rule could relax to "account for every grain column that has more than one distinct value present". Also revisit if a contract ever gains an explicit way to declare a grain column as a lens rather than a population, which would let the rule narrow to the columns that need it.
