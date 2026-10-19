# ADR-027: A tool may default a reporting lens, never a population

Date: 2026-08-19
Status: Accepted

Resolves the consequence [ADR-024](adr-024-plans-account-for-every-grain-column.md) recorded and left open.

## Context

ADR-024 requires a plan to account for every column in the fact's grain, and recorded the cost: *"the target question is under-specified as usually phrased. Answering it requires pinning `marketplace_key`, `distributor_view` and `selling_program`, and the tool must supply those and say so in the narrative."*

Building the measurement tool made that instruction concrete, and it does not survive contact with all three columns equally.

**`distributor_view` and `selling_program` describe the same units two ways.** A MANUFACTURING pull and a SOURCING pull are the same shipments under two lenses; RETAIL, BUSINESS and FRESH are the same again. Choosing one narrows how a figure is *read*. Nobody asks a question in these terms — they are vendor-reporting concepts, not business ones — so requiring them means refusing every naturally-phrased question.

**`marketplace_key` is a different population.** Defaulting it to `ATVPDKIKX0DER` answers about the United States. For a brand selling in Germany that is not a narrower reading of their question, it is an answer to somebody else's — and it arrives denominated in USD, because `currency_code` travels with the amount ([ADR-021](adr-021-money-column-shape.md)). A German brand would be shown dollars they never earned, and every figure would be individually correct.

So ADR-024's instruction is right about two of the three columns and wrong about the third, and the difference is not visible from the contract: all three are grain columns, all three have real source paths, and none is flagged as anything.

## Decision

**A tool may default a grain column that selects a *view* of the same units. It must never default one that selects *which* units.**

Expressed as a declared map, `LENS_DEFAULTS` in `domain/measurement/answers.py`, holding `distributor_view: MANUFACTURING` and `selling_program: RETAIL` and nothing else. Three properties make it more than a convenience:

**A column absent from the map is left unaccounted, so ADR-024 refuses the plan and names it.** The refusal is the mechanism, not a fallback — `marketplace_key` cannot acquire a silent default by omission, only by somebody adding it to a map whose docstring says not to.

**Every default applied is stated in the narrative, with its consequence.** Not "MANUFACTURING was assumed" but that these are views of the same units and a total across them would double-count. A default a caller cannot see is a population they did not choose.

**A caller's own value is never overridden.** The defaults are computed from the columns the request left open, so naming `distributor_view: SOURCING` keeps it. A default that silently replaced an explicit choice would answer a different question with internally consistent figures.

`MANUFACTURING` is defensible: it is the only distributor view that reconciles to AMC retail purchases and the only one under which `orderedRevenue` and `orderedUnits` are non-null. **`RETAIL` is genuinely arbitrary** and the narrative names it for that reason, rather than presenting it as the obvious reading.

## Consequences

**The target question is answerable as asked, minus a marketplace.** A caller supplies the metric, the grain, the window and one marketplace; the tool supplies the lenses. That is the smallest request that cannot be wrong.

**A marketplace is effectively a required argument**, and the refusal that enforces it is ADR-024's, unchanged. The cost is real: a user asking "what did I sell last week" gets a refusal rather than an answer until something upstream resolves which marketplace they mean. That is the right direction to be wrong in — a refusal names what is missing, and a defaulted marketplace does not.

**The distinction lives in one place and has to be applied by hand.** Nothing derives "lens" from a contract, because nothing in a contract expresses it. A new fact with a new option column will need a judgement, and the map is where that judgement is recorded.

**Multi-marketplace questions are not solved by this**, only made explicit. A brand selling in three marketplaces still cannot ask one question spanning them, because summing across `marketplace_key` mixes currencies and `Money` refuses that. It becomes three questions, or one question after the semantic layer can convert at the right date's rate (ADR-004).

## Alternatives rejected

| Rejected | Why |
|---|---|
| **Default all three, as ADR-024's consequence literally suggests** | The instruction was written before the columns were examined individually. Defaulting a marketplace answers a different population's question in the wrong currency, and every figure looks right |
| **Require all three** | Refuses every naturally-phrased question over concepts no user thinks in. It also pushes the choice onto whatever assembles the request, which is the model — so the lens would end up model-chosen, which is worse than tool-chosen and documented |
| **Infer the marketplace from what is loaded** | Attractive, and it fails in the case that matters: a brand that sells in one marketplace gets a right answer, a brand that sells in three gets whichever the registry happens to list first. It also makes the same question answerable one week and ambiguous the next, which is the objection ADR-024 already recorded against runtime option detection |
| **Default the marketplace but state it, like the lenses** | The disclosure is not equivalent. "Pinned to MANUFACTURING" tells a reader which view of their own data they are seeing; "pinned to the US" tells a reader they asked about the wrong country, which is not a caveat but a wrong answer with a footnote |
| **Add a `lens: true` flag to the contract** | Considered and rejected for the same reason ADR-024 rejected it: a declaration an author can forget, on exactly the columns where forgetting is invisible. A map in one file that a reviewer reads is more likely to stay right than a flag on eleven contracts |

## Review trigger

Revisit when a customer's marketplace can be resolved from their own configuration rather than from the question. At that point the marketplace stops being a question argument and becomes context, and this ADR narrows to "a tool may default a lens; a population comes from configuration, never from a default".

Also revisit when a second fact reaches the measurement path with option columns of its own. Two entries in the map is a list; a third with a different shape of judgement is a sign the distinction wants expressing somewhere a contract author can see it.
