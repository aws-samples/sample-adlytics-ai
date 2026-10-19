# ADR-111: Forbid runtime imports of producers and ad-platform clients

Date: 2026-10-06
Status: Proposed. The owner approved the business-outcomes design on 2026-10-06. Shared with the
forecasting design. Implemented (2026-10-08): the contract is in `pyproject.toml` and `make lint-boundaries`
runs it. The current design is in
[Business outcome measurement](../concepts/business-outcome-measurement.md).

## Context

The product is read-only: nothing writes to Amazon, activates an audience or changes a budget or bid. Today
that rule is enforced by review only. The import-linter contracts in `pyproject.toml` cover layering, no AWS
in the domain, no AWS in ports, and no runtime import of the validation client. None forbids an ad-platform
client. The forecasting design adds a customer-run producer under `producers/` and proposes a contract for
`producers.amazon_ads` only.

## Decision

**One `forbidden` import-linter contract keeps the runtime from importing any producer or ad-platform SDK.**

```toml
[[tool.importlinter.contracts]]
name = "Runtime never imports a producer or an ad-platform client"
type = "forbidden"
source_modules = ["domain", "ports", "adapters"]
forbidden_modules = ["producers"]   # plus every ad-platform SDK module added as a dependency
```

1. `include_external_packages = true` is already set (`pyproject.toml:146`), so an external SDK can be listed by module name.
2. Adding an ad-platform SDK as a dependency needs the owner's approval (working agreement), and the same
   change appends its module here.
3. One contract serves both designs. Whichever lands first adds it; the other extends `forbidden_modules`.

## Consequences

- `make lint-boundaries` fails any runtime import of a producer or listed SDK.
- import-linter 2.13 accepts a forbidden module that isn't in the import graph yet, so the contract landed
  before any `producers/` package exists. Checked on 2026-10-08: with no `producers` package the contract is
  kept; a temporary `import producers` in `adapters` and a `from producers.amazon_ads import …` in `domain`
  each broke it, naming the importing module and line.
- The forecasting design's [ADR-097](adr-097-ship-one-first-party-connector-per-source-as-its-own-stack.md)
  (Proposed) places first-party connectors under `connectors/`. If it's accepted, the change that creates
  `connectors/` appends that module to `forbidden_modules` here.

## Alternatives considered

- **Review only.** The status quo; it relies on every reviewer noticing.
- **A contract per producer.** Duplicates one rule and lets a new producer be forgotten.

## Review trigger

Reopen when a second producer or the first ad-platform SDK is added.
