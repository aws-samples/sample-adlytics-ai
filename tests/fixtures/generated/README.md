# Generated fixtures

GENERATED FROM `contracts/*/*.yaml` BY `make generate-fixtures`. Do not edit.
`make check-fixtures` regenerates these and fails on any diff, so an edit here is reverted
rather than kept. Change the contract instead.

These are **source-shaped**: they look like what Amazon delivers, not like the curated
table. A fixture that already looked like the target would skip the only step under test.

Monetary values are in the **scaled** space — millicents or microcents as the contract's
`scale_divisor` says — so a missing or wrong divisor fails a test instead of passing
quietly.

Every fixture has a `.manifest.json` sibling. A load with no manifest has nothing to
validate against; see `docs/concepts/provenance-chain.md`.

Each AMC fixture is laid out as its governed query pack's result: the pack's
`outputColumns`, with the three suppression columns last in the order AMC was observed to
deliver them, and an unsuppressed row's `distinct_user_count` at the pack's bar
(`suppression_output` in `contracts/shared/amc-columns.yaml`). It is written in AMC's CSV
dialect, a backslash-escaped quote inside a quoted value (`src/adapters/amc_csv.py`).
`make check-fixtures` holds every AMC fixture to the pack, the column registry and that live
shape; the `conformance` section of `contracts/shared/fixture-provenance.yaml` holds the only
exceptions, and each is reported.

These fixtures are generated from our own declarations, so they confirm a reader handles the
shape we believe Amazon sends. They are not evidence about what Amazon actually sends. The
hand-built trap fixtures and gitignored live validation evidence cover behaviors these
generated declarations cannot establish.
