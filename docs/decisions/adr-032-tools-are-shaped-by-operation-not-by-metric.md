# ADR-032: Tools are shaped by operation, and the planner grows plan kinds

Date: 2026-08-20
Status: Accepted

Extends [ADR-028](adr-028-a-validated-plan-replaces-the-template-registry.md), which chose a validated plan over a registry of SQL templates. That decision said what crosses the query port. This one says how many tools sit above it, and where a new capability lands when one is needed.

## Context

The metric dictionary declares 50 metrics — 26 simple, 24 ratio — derived from every measure column the canonical model carries. Two shapes of tool surface could serve them, and both are wrong in ways worth naming, because the wrong one is the intuitive one.

**A tool per intent** — `getCampaignROAS`, `getProductRevenue`, `compareEntities` — reads well and picks reliably. It is also how the platform's central guarantee gets lost. In this domain the guards *are* the product: grain accounting ([ADR-024](adr-024-plans-account-for-every-grain-column.md)), lens-not-population ([ADR-027](adr-027-a-tool-may-default-a-lens-never-a-population.md)), a ratio's operand grain intersection, measurement-basis mixing, source mixing, suppression, restatement. A per-intent tool hand-rolls each of those, once per tool.

The concrete failure is already recorded. "What is my ROAS by product?" is a reasonable question and a wrong figure: `fct_ad_exposure` has no product dimension, so it divides product-level revenue by spend broadcast across products. `getProductROAS` is exactly the function somebody writes, and it would pass review — the SQL joins real facts on real keys and returns real-looking numbers. What caught it was `make check-lineage` comparing each ratio's *declared* grains against what its operands can supply. That check is only possible because the grains are declared somewhere a gate can read. A hand-written tool has nothing to compare against, because its grain is implicit in a `GROUP BY` and no gate walks SQL.

**One generic tool** fails the other way. `metric_by_grain` accumulating optional fields — a comparison window, an ordering, a top-N, a second metric — becomes a request object whose invalid combinations are discovered at runtime rather than being unrepresentable, and a model cannot tell from the schema which combinations mean anything.

There is also a subtler version of the per-intent mistake that survives the first argument: a tool per *metric kind*. A `measurement_ratio` beside `measurement_metric_by_grain` looks like separation of concerns. It is not — whether a metric is simple or a ratio is a fact the dictionary owns, so making the model choose a tool on it pushes a dictionary fact into the model's decision, and the model will get it wrong and have to recover from a "not applicable" error it could not have predicted.

## Decision

**One tool per operation shape. The metric, the dimensions, the window and the filters are parameters. When a new metric shape needs computing, the planner grows a plan kind and the tool surface does not change.**

An *operation* is a question shape with its own output columns and its own mandatory caveats. A breakdown, a period comparison and a ranking are three operations. ROAS and CTR are not operations; they are arguments.

Two consequences fall straight out, and both are the point rather than side effects:

- **A tool accepts any declared metric**, simple or ratio. The planner routes on kind.
- **A plan kind is a domain concept, not a tool.** `QueryPlan` carries several aggregates and a `Combination` stating how they reduce to one figure, which is why a same-fact ratio and a difference of sums are the same change rather than two.

Four classes of metric follow from the model, and they map to planner capability rather than to tools. They partition all 50:

| Class | Planner capability | Count |
|---|---|---|
| Simple, one column | single-fact aggregate | 25 |
| Expression over two columns of one fact | difference of sums | 1 |
| Ratio, operands on one fact | two aggregates, one query, no join | 15 |
| Ratio, operands on two facts | drill-across, joined on shared dimensions | 9 |

> **Amendment (2026-09-17).** A fifth class exists: **product of operands**
> (`type: product`, exactly two declared metric operands). Its one member,
> `returns_value_estimate_retail`, is deliberately **not plannable** — a product's honest
> figure is derived at publish time from its already-measured operands (the baked-artifact
> path), and the planner refuses it with the operands offered instead, the same typed
> refusal shape as a drill-across. The declaration exists so the dictionary governs the
> metric's meaning and the focus dialog can serve its definition line; the counts above are
> asserted, at their current values, by
> `tests/unit/test_planning.py::TestTheMetricTaxonomyMatchesWhatIsDocumented`.

Two further properties cut across those classes rather than partitioning them, which is why they are listed separately — a metric has a combination *and* may also be non-additive or scaled:

| Property | Planner capability | Count |
|---|---|---|
| Non-additive | grain-locked, aggregation refused | 3 — `reach_users` and the two ratios over it |
| Scaled | a declared `multiplier` | 1 — `cpm` |

## Consequences

**The tool count is bounded by question shapes, not by the catalogue.** Fifty metrics, and the surface is three measurement tools and three catalog tools. Declaring a fifty-first costs a dictionary entry.

**A guard is written once and cannot be re-implemented wrongly.** Every metric reaches Athena through one `plan()`, so ADR-024's grain accounting, ADR-027's lens rule and the non-additivity refusal apply to all fifty without any tool opting in. This is the whole reason the surface is narrow.

**Model tool-selection stays reliable, because the surface stays small.** The advantage a per-intent library has — the tool name *is* the intent — is preserved for the operations that genuinely differ and spent nowhere else.

**The response shape does not move when a plan kind lands.** Aggregates arrive under numbered aliases and are combined into the single output key the envelope carries, so a caller never sees an operand. Adding a plan kind is invisible upward.

**A refusal is the honest output for a shape the planner cannot express**, and the refusal names the operands so the caller can ask for them separately. A cross-fact ratio refuses today for exactly this reason rather than returning one side of itself.

**The cost is indirection.** "Which tool answers ROAS?" has no direct answer — `measurement_metric_by_grain` does, with `metric_name="roas_attributed_14d"`. That is a real readability loss against a `getROAS`, and `catalog_list_metrics` exists partly to pay it back: the vocabulary is discoverable rather than implied by a tool list.

## Alternatives rejected

| Rejected | Why |
|---|---|
| **A tool per metric or per subject** | The guards are re-implemented per tool, and one of them will be wrong. `getProductROAS` is the worked example: computable, plausible, wrong, and it would pass review because no gate can read a grain out of a `GROUP BY` |
| **One tool with optional fields for every operation** | Invalid combinations become runtime failures instead of being unrepresentable, and nothing in the schema tells a model which combinations mean anything |
| **A separate tool per metric kind** — `measurement_ratio` beside the aggregate tool | Pushes a fact the dictionary owns into the model's tool choice. The model cannot know a metric's kind before calling, so it would recover from avoidable errors |
| **A second plan *type* for ratios** | Considered, and it is what "drill-across is a second kind of plan" in ADR-028 sounded like. A same-fact ratio and a difference of sums are both several aggregates over one fact, so a second type would duplicate the whole plan to vary the arithmetic. The plan widened instead |
| **Generating a template per metric-and-grain from the dictionary** | ADR-028 rejected this already. Repeated here because the metric count makes it tempting again: fifty metrics across six grain sets is hundreds of templates, and a template is a cache of a derivation |
| **Letting the model compose a ratio from two tool calls** | The envelope's first rule is that the model never divides. Two calls and a division is that rule broken with extra steps, and the operand grains would go unchecked |

## Review trigger

Revisit when an operation appears whose *output columns* differ from a breakdown, a comparison and a ranking — the shape test, not the subject test. A funnel or a cohort would qualify; a new metric never does.

Revisit the plan-kind half if a fifth combination appears that is neither a reduction of aggregates over one fact nor a join of two. Drill-across was the known next one and it landed as predicted — a join rather than a reduction, so it is the one plan kind that added to `QueryPlan`'s shape (`JoinSide`) rather than widening `measures`. A third fact in one ratio would be the next real test, and nothing in the dictionary needs one.
