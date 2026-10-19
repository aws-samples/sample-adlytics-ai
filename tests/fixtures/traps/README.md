# Trap fixtures

**Hand-built, and deliberately not in `../generated/`.** `make generate-fixtures` deletes any
file in that directory it did not write, so these live here. They are also the one kind of
fixture that *should* be hand-written: each encodes a specific known behaviour, each is a few
rows, and each is worth more than a hundred generated ones — a generated fixture can only
confirm our code does what we intended, because it is built from our own declarations.

The six cases are enumerated in the table below; the hand-built-trap rule is owned by the
`meta-testing-strategy` steering. Assertions are in `tests/unit/test_curation_traps.py`.

| Fixture | Contract | What it traps |
|---|---|---|
| `amc-suppression-merged.csv` | `amc-dsp-impressions` | Metrics survive suppression and are SUM-re-aggregated; the distinct count is **blank** on the merged row and must become NULL rather than zero. The merged row keeps its keys, so it is the in-fact withheld row ADR-035 folds |
| `vendor-restatement-earlier.json` + `-later.json` | `vendor-sales-by-asin` | The same period delivered twice with different `lastUpdatedDate`. Same grain, corrected figures — a load must overwrite, never append, and never revert to the older one. Its day (2025-06-11, watermarks 2025-06-18 and 2025-06-25) deliberately precedes the generated fixture span (2025-07-01 onward): a trap that must stay CURRENT for an end-to-end assertion cannot share a partition day with full-span generated landings, which restate it away (observed 2026-09-16) |
| `vendor-sourcing-nulls.json` | `vendor-sales-by-asin` | `distributorView: SOURCING` omits `orderedRevenue` and `orderedUnits` entirely |
| `vendor-fresh-nulls.json` | `vendor-sales-by-asin` | `sellingProgram: FRESH` omits `customerReturns` |
| `vendor-negative-units.json` | `vendor-sales-by-asin` | Negative `orderedUnits`. Cancellations are booked at cancellation time, so this is correct data |
| `vendor-mixed-currency.json` | `vendor-sales-by-asin` | Two currencies in one document, so no single `currency_code` is right for the rollup |
| `vendor-control-total-consistent.json` | both vendor sales contracts | **Both grains in one document**, which is what a real report carries. The three detail rows sum to the reported aggregate |
| `vendor-control-total-short.json` | both vendor sales contracts | The by-ASIN array is truncated. Every surviving row is correct, the reported total is correct, and the sum of the parts is quietly short |
| `vendor-hierarchy-matching-control-total.jsonl` | `vendor-product-hierarchy` | Covers exactly the ASINs in the two control-total fixtures, in one marketplace, so a dimension-resolution check over that pair can actually **pass** |

**Rule: a trap that must stay CURRENT covers no day in `fixture_span`** (the synthetic catalog's `shape.fixture_span`). Every Vendor trap was re-dated by −420 days (60 weeks, keeping weekdays) on 2026-10-05, with `retrieved_at` unchanged, so each lands at a new prefix. Partitions earlier landings left CURRENT on span days are superseded by `_RETIRED_TRAP_PARTITIONS` in `src/adapters/jobs/land_fixtures/bundle.py`, and `test_11_no_trap_hides_a_span_day_and_every_retired_trap_is_superseded` enforces both.

**Every AMC trap is in AMC's live shape** (owner S-8, 2026-10-08; [Sources/AMC](../../../docs/sources/amc.md) §5): the three option columns last as `distinct_user_count, suppression_reason, rows_suppressed`, the bar (2) on an unsuppressed row, and on a suppressed row a count equal to its reason's, the unprefixed reason form, and every LOW source column NULL, currency included. Only what a trap's purpose needs still differs, and `trap_deviations` in `contracts/shared/fixture-provenance.yaml` is the one list of those differences and their basis, held by `make check-fixtures`. When the landed AMC traps took this shape on 2026-10-09, each `retrieved_at` moved one day later, so the new bytes land at new prefixes instead of colliding with the earlier landings.

## Two defects these found before any assertion was written

Both would have quarantined partitions that were behaving exactly as designed, and neither was
reachable with a generated fixture, because a generated fixture never contains an absent value.

**`distinct_user_count` was declared required on all six AMC contracts.** AMC leaves it blank
on a merged row — the count that drove the decision is not reported for a row that was
merged — as the delivered CSV in [provenance-chain](../../../docs/concepts/provenance-chain.md)
§2 shows. Required, it failed the row model and `IsComplete` in DQDL. Now nullable with
`null_meaning: suppressed`.

**The CSV reader treated an empty field as an empty string.** CSV cannot spell null, so
absence arrives as `,,`. Left as `""` a nullable string stored an empty string where the
contract says the value was withheld, and a nullable integer failed to parse at all. The
reader now maps an empty field to `None`, accepting that a genuinely empty string becomes
null — the right way round when the four meanings of null are load-bearing.

## Generated fixtures align identities but not cross-contract relationships

The shared synthetic entity catalog now gives generated contracts the same fictional product,
marketplace, and source-qualified campaign identities. That removes accidental key mismatch; it
does not make independently generated values reconcile. Contracts still declare no relationship
such as detail rows summing to a reported total, a later delivery restating an earlier one, or two
campaign aliases being intentionally ambiguous.

Any test asserting a relationship across contracts therefore still needs a hand-built, mutually
consistent input. That is what `vendor-hierarchy-matching-control-total.jsonl` and the paired control
total scenarios are for.

## Why the control total needed a hand-built document at all

`make generate-fixtures` emits **one array per contract**, because a contract is one source
dataset at one grain — so `vendor-sales-by-asin.json` has only `salesByAsin` and
`vendor-sales-reported-total.json` has only `salesAggregate`. The control-total assertion
compares the two grains *of one document*, so no generated fixture can exercise it. That is a
structural limit of generating from contracts rather than an oversight in the generator.

## What is deliberately not trapped here

**A suppressed row whose *dimension* was nulled is now a verified behavior, not an open fixture question.** [verified 2026-08-21] AMC nulls each output column against its own threshold; a one-user row loses LOW campaign/date/marketplace values while additive measures survive. `amc-withheld-residual.csv` exercises the delivery-keyed residual path, its withheld row's currency NULL so its spend is withheld rather than summed (AMC-V decision 5, [canonical model](../../../docs/canonical-model.md) §4a), and non-additive reach suppression is represented separately because any suppressed row blocks that delivery. [Sources/AMC](../../../docs/sources/amc.md) §5 owns the behavior and [validation results](../../../validation/RESULTS.md) owns the executions.

**The option columns use the canonical aliases produced by the governed extraction.** AMC's raw option names are configured to deliver `rows_suppressed`, `distinct_user_count`, and `suppression_reason`; fixtures model the producer-delivered result rather than an unconfigured AMC default.

No real customer data, account IDs or ASINs tied to a real advertiser. Every ASIN here is
`B0TRAP*` and every campaign identifier is synthetic.
