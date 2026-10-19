# ADR-055: Govern temporal attribution as an exact selector

Date: 2026-08-24
Status: Accepted

## Context

A deduplicated Amazon Ads ROAS numerator can assign each matched conversion by first touch, last
touch, or linear credit. These are alternative interpretations of the same conversion population,
not additive measures. Spend is independent of that interpretation and must not be repeated once per
model.

The model choice must remain visible from producer SQL through the curated fact and semantic query.
If the producer, data contract, and metric vocabulary drift, a valid-looking request can select a
model the data never produced or silently omit one that was promised.

## Decision

**Store one row family with `attribution_model_key` and require callers to select exactly one
registered model.**

The versioned registry is the owner of model keys and definitions. The data contract declares the
same closed value domain, and the governed query-pack loader proves that the SQL branches aliased as
`attribution_model_key`, the contract domain, and the registry keys are equal before a pack can be
generated or executed. Missing, extra, and repeated branches fail closed.

`attribution_model_key` is an exact-only, filterable, non-groupable, non-alignable semantic axis. A
metric requiring it has no default. Missing and unknown values are refusals; exact registered values
are accepted.

The deduplicated ratio applies the model predicate only to its numerator. Model-independent spend is
aggregated once on the denominator side. The fact stores allocated additive purchase credit and
halo-inclusive revenue; ROAS remains a ratio computed by the semantic layer.

## Consequences

One fact and one metric family support first-touch, last-touch, and linear interpretation without
tripling spend or multiplying schemas. Adding a model requires an explicit registry, contract, SQL,
and validation change, so the change is reviewable and cannot partially ship.

Two differently selected models cannot be combined as though they described independent
populations. Comparisons request each exact model separately and label each result with its selector.
The existing advertised-product ROAS remains the default; deduplicated revenue is explicitly
halo-inclusive because the conversion identity source does not expose advertised-product-only
revenue.

The final producer grain remains conversion date × channel × marketplace × currency × model. The
producer rejects any suppressed delivery because suppression removes the dimensions and currency
needed to store a monetary residual safely.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Separate first-, last-, and linear-touch columns | Multiplies schema and metric definitions, makes extension expensive, and encourages combining alternative interpretations |
| Separate fact per model | Duplicates governance and curated-table behavior without changing the population or source |
| Encode the model in metric names only | Hides the selected interpretation from planning and makes numerator-only filtering unverifiable |
| Copy spend into each model row | Triples model-independent spend and produces plausible but incorrect totals |
| Default to one model | Silently answers an ambiguous question and prevents deterministic model comparison |
| Filter both ratio operands by model | Spend has no attribution model; applying the predicate would either fail or invent a model-specific denominator |

## Review trigger

Revisit if AMC exposes a stable conversion identity with advertised-product-only revenue, if a model
uses a different eligible conversion population or lookback window, or if a future comparison needs
a first-class multi-model result contract rather than separate exact requests.
