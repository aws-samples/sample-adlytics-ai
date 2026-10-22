# SPDX-License-Identifier: MIT-0
"""Write the generated fixtures to disk, or verify they are not stale.

`make generate-fixtures` writes them; `make check-fixtures` regenerates and fails on any
diff. The pairing matters more here than it does for the schema. A stale fixture does not
break a build — it makes tests keep passing against a source shape that no longer exists,
which is worse than a failure because it reads as evidence.

Fixtures are committed rather than generated during the test run. A test suite that
generates its inputs proves only that the generator agrees with itself, and reviewing a
change to a fixture is how a change to a source shape becomes visible in a diff.

Serialisation lives here rather than in `shapes.py` because it is an adapter concern: the
same flat rows become a CSV for AMC and JSONL for Data Kiosk, and the shape does not care.

Stdout is the interface, the same as the other gates: a command-line tool run by a make
target, not a service.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from adapters.amc_csv import write_rows
from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.fixture_generation.audience import AUDIENCE_CONTRACTS, build_audience
from adapters.jobs.fixture_generation.catalog import load_synthetic_entity_catalog
from adapters.jobs.fixture_generation.checks import check_generated_shape
from adapters.jobs.fixture_generation.cohorts import COHORT_CONTRACTS, build_acquisition_cohorts
from adapters.jobs.fixture_generation.conformance import (
    ConformanceRun,
    inputs_from,
    load_suppression_output,
    result_shape,
    run_conformance,
)
from adapters.jobs.fixture_generation.daily import DAILY_BOUNDS, DailyInputs, build_daily
from adapters.jobs.fixture_generation.deduplicated import (
    DEDUPLICATED_BOUND,
    DEDUPLICATED_CONTRACT,
    DeduplicatedInputs,
    build_deduplicated,
)
from adapters.jobs.fixture_generation.exposure import (
    DSP_CORPUS,
    EXPOSURE_CONTRACT,
    SPONSORED_CORPUS,
    ExposureCorpus,
    build_exposure_groups,
)
from adapters.jobs.fixture_generation.frequency import (
    FREQUENCY_CONTRACTS,
    build_exposure_frequency,
)
from adapters.jobs.fixture_generation.periodic import (
    BUYER_BOUNDS,
    PeriodicCorpus,
    build_periodic,
)
from adapters.jobs.fixture_generation.profile import PROFILE_CONTRACTS, build_segment_profile
from adapters.jobs.fixture_generation.provenance import (
    load_fixture_evidence_manifest,
    validate_fixture_provenance,
)
from adapters.jobs.fixture_generation.shapes import Fixture, FixtureBuildOptions, build
from adapters.jobs.fixture_generation.vendor import (
    reconcile_vendor_aggregates,
    reported_units_adjustments,
)
from adapters.producer.packs import load_query_packs
from domain.data_contracts.contract import DataContract, SourceFormat

OUTPUT_DIR = pathlib.Path("tests/fixtures/generated")

README = """\
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
"""


def _csv_cell(value: Any) -> str:
    """Render one value the way a delivered CSV carries it."""
    if value is None:
        return ""
    if isinstance(value, bool):
        # Lowercase, as SQL engines emit. `str(True)` would be "True", which no reader
        # parsing a delivered file should have to accept.
        return "true" if value else "false"
    return str(value)


def render(fixture: Fixture) -> str:
    """Serialise a fixture into the bytes its source format would deliver."""
    if fixture.source_format is SourceFormat.JSON_DUAL_ARRAY:
        return json.dumps(fixture.document, indent=2) + "\n"

    if fixture.source_format is SourceFormat.JSONL:
        return "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in fixture.rows)

    # CSV, in AMC's dialect: a free-text value holding a quote is quoted with the quote
    # backslash-escaped, the form AMC delivers (`adapters.amc_csv`). LF line endings, because a
    # committed fixture with CRLF endings produces a diff on every platform that writes LF.
    header = list(fixture.rows[0]) if fixture.rows else []
    return write_rows(
        header,
        ({key: _csv_cell(value) for key, value in row.items()} for row in fixture.rows),
    )


def _files_for(fixture: Fixture) -> dict[str, str]:
    """Map every path this fixture owns to its content, relative to the output directory."""
    return {
        f"{fixture.contract_id}.{fixture.extension}": render(fixture),
        f"{fixture.contract_id}.manifest.json": json.dumps(fixture.manifest, indent=2) + "\n",
    }


def _coalesce_source_documents(
    fixtures: tuple[Fixture, ...], units_adjustments: Mapping[str, int] | None = None
) -> tuple[Fixture, ...]:
    """Give every sibling contract for one Vendor document the complete source shape.

    Sales and traffic each expose two arrays within one source document. Their contracts remain
    separate because their curated grains differ, but emitted fixtures must remain one complete
    delivery shape: landing a partial sibling beside another partial sibling would collide at one
    raw identity and cannot exercise both readers from one report.
    """
    groups: dict[tuple[str, str], list[Fixture]] = {}
    for fixture in fixtures:
        if fixture.source_format is SourceFormat.JSON_DUAL_ARRAY:
            key = (str(fixture.manifest["source_system"]), str(fixture.manifest["source_dataset"]))
            groups.setdefault(key, []).append(fixture)
    completed: dict[str, dict[str, Any]] = {}
    for siblings in groups.values():
        if len(siblings) == 1:
            continue
        document: dict[str, Any] = {}
        for sibling in siblings:
            if sibling.document is None:
                raise ValueError("JSON dual-array fixtures require a source document.")
            for key, value in sibling.document.items():
                if key == "reportSpecification":
                    if key in document and document[key] != value:
                        raise ValueError("sibling fixture report specifications must agree.")
                    document[key] = value
                elif key in document:
                    raise ValueError("sibling fixture arrays must be distinct.")
                else:
                    document[key] = value
        document = reconcile_vendor_aggregates(document, units_adjustments)
        for sibling in siblings:
            completed[sibling.contract_id] = document
    return tuple(
        replace(fixture, document=completed[fixture.contract_id])
        if fixture.contract_id in completed
        else fixture
        for fixture in fixtures
    )


def _problems(expected: dict[str, str], target_dir: pathlib.Path) -> list[str]:
    """Name every fixture on disk that disagrees with the contracts.

    Every problem at once rather than the first, for the same reason `getConfig` and the
    contract gate do it: finding the next one on the next run is the cost this avoids.

    Orphans count as problems. A renamed contract leaves its old fixture behind, and a
    fixture with no contract is the drift this whole pairing exists to prevent.
    """
    found: list[str] = []
    for name, content in sorted(expected.items()):
        path = target_dir / name
        if not path.exists():
            found.append(f"missing: {OUTPUT_DIR / name}")
        elif path.read_text() != content:
            found.append(f"stale:   {OUTPUT_DIR / name}")
    if target_dir.exists():
        for path in sorted(target_dir.iterdir()):
            if path.is_file() and path.name not in expected:
                found.append(f"orphan:  {OUTPUT_DIR / path.name}")
    return found


def _report_conformance(run: ConformanceRun) -> bool:
    """Print every applied exception and every refusal; True when nothing was refused."""
    print("\nFIXTURE CONFORMANCE (packs, column registry, live suppression shape)")
    for marker in run.markers:
        print(f"  {marker}")
    for deviation in run.trap_deviations:
        print(f"  {deviation}")
    if run.findings:
        print(f"\n{len(run.findings)} conformance finding(s):")
        for finding in run.findings:
            print(f"  {finding}")
        return False
    print("  no conformance finding")
    return True


ATTRIBUTED_CORPUS = "amc-attributed-conversion"
"""The attributed-conversion corpus whose purchase currency the cohort fixtures carry."""


def build_fixtures(root: pathlib.Path) -> tuple[Fixture, ...]:
    """Build every contract's fixture from the governed sources under `root`, in contract order.

    An AMC fixture is laid out as its governed pack's result (`ResultShape`), so the pack, the
    registry's live suppression shape and the contract all reach the bytes.
    """
    registry = load_registry(root)
    contracts = [contract for _, contract in load_contracts(root)]
    entity_catalog = load_synthetic_entity_catalog(root)
    options = FixtureBuildOptions(
        rows=entity_catalog.shape.fixture_span.span_days, entity_catalog=entity_catalog
    )
    suppression_output = load_suppression_output(root)
    packs = {pack.contract_id: pack for pack in load_query_packs(root)}

    def periodic(contract_id: str) -> bool:
        return contract_id in packs and packs[contract_id].period_policy is not None

    built = _coalesce_source_documents(
        tuple(
            build(
                contract,
                registry,
                replace(
                    options,
                    result=result_shape(packs[contract.contract_id], suppression_output)
                    if contract.contract_id in packs
                    else None,
                ),
            )
            for contract in contracts
            if not periodic(contract.contract_id)
        ),
        reported_units_adjustments(entity_catalog.shape),
    )
    # A periodic pack is one delivery per complete period, drawn from the attributed corpus
    # built above (ADR-100), or pack 5's grid from the DSP and sponsored corpora; a daily
    # distinct-count pack is one delivery per declared day, bounded by its corpus (pack 4);
    # every other fixture is built as before.
    by_id = {fixture.contract_id: fixture for fixture in built}

    def laid_out(contract: DataContract) -> Fixture:  # noqa: PLR0911 -- one return per layout
        contract_id = contract.contract_id
        if contract_id in AUDIENCE_CONTRACTS:
            return build_audience(
                packs[contract_id],
                (contract, suppression_output, options),
                by_id[DSP_CORPUS].rows,
            )
        if contract_id in PROFILE_CONTRACTS:
            return build_segment_profile(
                packs[contract_id], (contract, suppression_output, options)
            )
        if contract_id in FREQUENCY_CONTRACTS:
            return build_exposure_frequency(
                packs[contract_id],
                (contract, suppression_output, options),
                ExposureCorpus(by_id[DSP_CORPUS].rows, by_id[SPONSORED_CORPUS].rows),
            )
        if contract_id in COHORT_CONTRACTS:
            # Packs 14 and 15: one delivery per cohort vintage, in the attributed corpus's one
            # purchase currency (design §6.6).
            currencies = {str(row["purchase_currency"]) for row in by_id[ATTRIBUTED_CORPUS].rows}
            if len(currencies) != 1:
                raise ValueError(f"the attributed corpus carries currencies {currencies}.")
            return build_acquisition_cohorts(
                packs[contract_id],
                (contract, suppression_output, options),
                currencies.pop(),
            )
        if contract_id == EXPOSURE_CONTRACT:
            return build_exposure_groups(
                packs[contract_id],
                (contract, suppression_output, options),
                ExposureCorpus(by_id[DSP_CORPUS].rows, by_id[SPONSORED_CORPUS].rows),
            )
        if periodic(contract_id):
            return build_periodic(
                packs[contract_id],
                (contract, suppression_output, options),
                PeriodicCorpus(by_id[BUYER_BOUNDS[contract_id].corpus_contract].rows),
            )
        if contract_id == DEDUPLICATED_CONTRACT:
            # Every model on every day, splitting one day total of the attributed corpus
            # (dev-seeding §2).
            return build_deduplicated(
                by_id[contract_id],
                DeduplicatedInputs(
                    contract,
                    by_id[DEDUPLICATED_BOUND.corpus_contract].rows,
                    entity_catalog.shape.deduplicated_attribution,
                ),
            )
        if contract_id in DAILY_BOUNDS:
            return build_daily(
                by_id[contract_id],
                DailyInputs(
                    contract,
                    packs[contract_id],
                    suppression_output,
                    by_id[DAILY_BOUNDS[contract_id].corpus_contract].rows,
                    entity_catalog,
                ),
            )
        return by_id[contract_id]

    return tuple(laid_out(contract) for contract in contracts)


def main(argv: list[str]) -> int:
    """Write the fixtures, or with `--check` verify none is stale, missing or orphaned."""
    check_only = "--check" in argv
    root = pathlib.Path.cwd()
    target_dir = root / OUTPUT_DIR

    contracts = [contract for _, contract in load_contracts(root)]
    entity_catalog = load_synthetic_entity_catalog(root)
    packs = {pack.contract_id for pack in load_query_packs(root)}
    fixtures = build_fixtures(root)
    ties = check_generated_shape(entity_catalog)
    evidence_manifest = load_fixture_evidence_manifest(root)

    expected: dict[str, str] = {"README.md": README}
    print("FIXTURE GENERATION")
    print("=" * 70)
    span = entity_catalog.shape.fixture_span
    print(f"  span {span.first_day}..{span.last_day}")
    print(f"  reported, not gated (design §3.7 test 3): {ties}")
    for contract, fixture in zip(contracts, fixtures, strict=True):
        files = _files_for(fixture)
        expected.update(files)
        columns = len(fixture.rows[0]) if fixture.rows else len(fixture.document or {})
        print(
            f"  {contract.contract_id:34s} {fixture.source_format.value:16s} "
            f"{columns:3d} top-level  -> {contract.target_table}"
        )

    conformance = run_conformance(
        inputs_from(
            root,
            {
                f"{contract_id}.csv": expected[f"{contract_id}.csv"]
                for contract_id in sorted(packs)
                if f"{contract_id}.csv" in expected
            },
        )
    )
    if not _report_conformance(conformance):
        return 1

    if check_only:
        problems = _problems(expected, target_dir)
        if problems:
            print(f"\n{len(problems)} problem(s):")
            for problem in problems:
                print(f"  {problem}")
            print("\nRun `make generate-fixtures` and commit the result.")
            return 1
        validate_fixture_provenance(root, evidence_manifest, entity_catalog)
        print(f"\n{OUTPUT_DIR} matches its governed sources ({len(expected)} files).")
        return 0

    target_dir.mkdir(parents=True, exist_ok=True)
    for path in target_dir.iterdir():
        if path.is_file() and path.name not in expected:
            path.unlink()
    for name, content in expected.items():
        (target_dir / name).write_text(content)
    validate_fixture_provenance(root, evidence_manifest, entity_catalog)
    print(f"\nWrote {len(expected)} files to {OUTPUT_DIR}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
