# ADR-028: A validated plan replaces the allowlisted template registry

Date: 2026-08-19
Status: Accepted

Reverses the mechanism described in [provenance-chain](../concepts/provenance-chain.md) §2.1, [understanding-the-data-model](../understanding-the-data-model.md) §6, and [F-14](../decision-coverage-registry.md#finding-register-f-nn). The **invariant** those documents protect is unchanged.

## Context

Every one of those documents describes the same mechanism for keeping model-authored SQL out of the query path: a registry of named, pre-written SQL templates, which a tool selects from and fills with typed parameters. `QueryPort` was to take a template *name*.

Building the port made the cost visible.

**A template is one metric at one grain.** `retail_revenue_shipped` by product, by date, by marketplace, by product-and-marketplace, by date-and-product — five templates for one metric out of nineteen, before any filter combination. The registry is dozens of hand-written strings.

**Each of them restates what the metric dictionary already declares.** `contracts/shared/metric-dictionary.yaml` states that this metric is `sum(fct_retail_sales.revenue_shipped)` and is valid at `[date, product, marketplace]`. A template for it restates the fact, the column and the grain in SQL. Two declarations of one fact, and `make check-lineage` verifies the dictionary while nothing verifies the templates against it.

**So the registry reintroduces exactly the drift the dictionary exists to prevent.** A metric whose definition changes leaves every template naming it wrong, silently, because a template is a string and nothing walks it.

The invariant, restated so it is clear what must survive: **no SQL a model wrote or influenced may execute, and no query string may be built from a model-supplied value.**

## Decision

**`QueryPort` takes a `QueryPlan` — a validated structure carrying no SQL — and the adapter composes the statement.**

The allowlist still exists. It is expressed as a **structure that is checked** rather than a **registry of strings that is trusted**, and what it admits is exactly:

- a metric the dictionary declares, at a grain the dictionary declares valid for it
- a fact and a measure column resolved from that metric's own declared expression, never from a caller
- group-by columns drawn from a closed map of dimension names
- filter keys checked against the fact's real column list, and **rebuilt from the contract's own column tuple** so the identifier an adapter interpolates provably originates in a contract rather than being a caller's string that compared equal to one
- every grain column accounted for, so the aggregation cannot double-count ([ADR-024](adr-024-plans-account-for-every-grain-column.md))

The set of permitted queries is therefore *narrower* than a template registry's, not wider: a registry admits whatever somebody wrote into it, including a template whose grain the dictionary never blessed.

**The rule this leaves an adapter is: interpolate identifiers, parameterise values.** SQL has no placeholder for a table or column name, so identifiers are formatted into the text and that is safe only because a plan validated each one. Everything a caller supplied is a parameter.

## Consequences

**One declaration of what a metric is.** The dictionary, verified by `check-lineage`. Nothing restates it in SQL.

**The port's core guarantee is untouched and is still structural.** There is no way to pass a SQL string through `QueryPort`, and a `QueryPlan` carries none. That was the whole point of the template-name shape and it survives the change.

**The composed statement is asserted character by character in the unit tier**, because composition is a pure function of a plan. A template registry would have been asserted by reading it, which is not the same thing.

**A new metric-and-grain combination costs nothing.** No template to write, no registry entry to review. That is the benefit and it is also the risk: the set of answerable questions widens without a review step, which is why the plan's validation is where the scrutiny has to be, and why ADR-024's refusal exists.

**Four documents described the withdrawn mechanism for a period.** They are corrected in the same change as this ADR. The lesson is the one the working agreement already states — when a decision reverses an earlier one, every affected document changes with it — and the reason it was missed here is that the reversal was recorded in a module docstring, which no reader of the design docs sees.

**The word "templates" in two steering files now reads oddly** and has been reworded to the invariant rather than the mechanism. The rules those files state were satisfied throughout; only the noun was wrong.

## Alternatives rejected

| Rejected | Why |
|---|---|
| **Keep the registry, generate the templates from the dictionary** | The obvious repair, and it makes the registry a build artefact of the dictionary — at which point the registry is a cache of a derivation, and the derivation is the thing. That derivation is `compose()`, so this is the decision above with an extra file on disk |
| **Keep the registry, hand-written, and gate it against the dictionary** | A third gate verifying that a string restates a declaration correctly. Cheaper to not have the string |
| **Keep the template name and let a plan choose it** | Considered, because it preserves the documented shape. It needs a name per metric-and-grain anyway, so the registry's size problem returns unchanged, and the plan is then validated and *also* discarded in favour of a lookup |
| **Allow a tool to pass SQL, and validate the SQL** | Never on the table. Parsing SQL to decide whether it is safe is the problem this platform structurally avoids, and a parser that is wrong once is wrong in the direction of executing something |

## Review trigger

Revisit if a question arrives that a plan cannot express — a ratio spanning two facts is the known case, and `plan()` refuses it explicitly today. The answer there is drill-across (aggregate each operand, join the results), which is a second *kind* of plan rather than a return to templates. If a third kind appears, the shape of `QueryPlan` is what to reconsider, not the choice against strings.
