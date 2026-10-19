# ADR-051: Classify fixture provenance and evidentiary status

Date: 2026-08-24
Status: Accepted

## Context

The repository currently uses several kinds of test data under one informal word: fixture. They do
not have the same provenance and cannot support the same claim.

Contract-generated fixtures are derived from this repository's own declarations. They prove that a
reader handles the shape and scaling the repository declares, but they cannot prove that a source
actually emits that shape. Hand-built traps encode specific edge cases and relationships that a
schema generator cannot derive. Presentation fixtures demonstrate interactions and narration, not
source behavior. Live AMC results remain gitignored validation evidence.

[verified] The configured AMC environment is an internal demo environment rather than customer
production data. A successful STANDARD execution establishes compilation, output header, and observed
values only for that exact governed query in that configured demo instance. A SANDBOX execution can
settle whether an output shape or privacy operator is accepted with aggregation controls enabled, but
sandbox success does not establish advertiser-instance SQL admissibility. Documented identifier forms
remain source-document evidence; values observed in the demo remain examples rather than universal
lexical or population claims. Neither environment can establish customer volume, suppression
viability, naming distributions, match rates, or production outliers. [Sources/AMC](../sources/amc.md)
§§2 and 8 and [validation results](../../validation/RESULTS.md) own those boundaries.

Without an explicit classification, a source-shaped generated row can be described as source-derived,
a presentation fixture can drift from the ingestion fixture for the same entity, and a successful
sandbox scenario can be cited as production evidence. The resulting tests remain green while proving
a stronger claim than their inputs permit.

## Decision

**Every committed fixture declares one provenance class, and each class has a bounded evidentiary
status. Live source values are never copied into committed fixtures.**

The allowed classes are:

| Class | How it is produced | What it may prove |
|---|---|---|
| `schema_derived` | generated from governed contracts and registries | parser compatibility with the declared shape, deterministic generation, declared types, scale factors, and required headers |
| `source_invariant_synthetic` | fictitious values constructed from a source behavior that a source document or recorded narrow probe verified | the implementation handles that exact verified behavior; not its frequency or customer distribution |
| `consistency_scenario` | hand-built mutually consistent records spanning contracts or grains | reconciliation, joins, ambiguity, and other relationships that independent generation cannot express |
| `presentation_synthetic` | fictitious presentation responses built from governed metrics and entities | interaction, accessibility, narrative, and unavailable-state behavior; never source or query fidelity |
| `live_integration` | bounded execution against the configured source or deployed development resources | the exact adapter/query behavior and environment state recorded by the owning validation result; no committed payload |

Each fixture directory or machine-readable fixture manifest names its class and points to the owning
contract, source evidence, or scenario rule. A class may make only the claim in the table. Generated
headers remain contract-derived even when their row values come from a shared synthetic entity
catalog.

A single deterministic synthetic entity catalog supplies fictitious advertiser, campaign,
marketplace-alias, product, and brand identities to source-invariant, consistency, evaluation, and
presentation artifacts. It must represent verified lexical and relational traps, including distinct
DSP and sponsored campaign identifier forms, duplicate display names, renamed or missing names,
advertised versus converted products, and source-specific marketplace namespaces. Names are
attributes; stable identifiers remain keys.

The catalog uses reserved test prefixes or ranges and contains no customer name, account identifier,
campaign identifier, AMC instance identifier, ASIN tied to a real advertiser, or measured value from
a live result. Live downloads stay in gitignored validation storage. Committed evidence records the
invariant learned, the probe or source reference, and a fictional example constructed from it.

Privacy behavior is represented only by threshold-consistent source-invariant scenarios. Arbitrary
alternating suppression flags in schema-generated rows are not evidence of source behavior. A
non-additive result under suppression remains a refusal scenario rather than a plausible aggregate.

## Consequences

Fixture review can distinguish structural coverage, behavioral evidence, cross-contract consistency,
presentation behavior, and real integration. A green schema-derived test no longer implies live
source conformance.

The generated fixture system remains deterministic and contract-driven. It needs a governed value
provider for source-specific lexical forms, while relationship-heavy and privacy scenarios remain
small explicit fixtures.

Screen and agent evaluations can share identities with ingestion tests without importing raw source
rows. A campaign displayed in a presentation scenario can therefore exercise the same identifier,
name ambiguity, marketplace mapping, and product-role semantics as its source-shaped counterpart.

The live environment remains necessary for source and adapter acceptance. It is not replaced by more
realistic-looking synthetic data, and its demo volumes are never promoted to customer behavior.

Fixture and screen privacy gates must scan every committed format that can carry identifiers, not
only JSON. Violations fail locally before an artifact is reviewed.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Commit anonymized copies of live rows | De-identification can leave customer or demo-account relationships, and copied values become stale evidence with unclear custody |
| Treat every generated source-shaped row as source-derived | The generator reads repository declarations, so it can only confirm the repository agrees with itself |
| Generate every edge case | Contracts do not declare cross-field relationships, ambiguity, or source privacy behavior; generating them would invent semantics in the generator |
| Hand-author all fixtures | Creates another schema authority that silently drifts from contracts |
| Use only the AMC sandbox | It does not cover Vendor data and cannot establish production distributions or every advertiser-instance behavior |

## Review trigger

Revisit when a source supplies a formally licensed, privacy-safe conformance corpus whose custody and
allowed evidentiary claims are stronger than invariant-derived synthetic data, or when a new fixture
class cannot be classified without weakening an existing class.
