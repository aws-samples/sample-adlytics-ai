# SPDX-License-Identifier: MIT-0
"""Generated AMC fixtures conform to their packs, the column registry and the live AMC shape.

A generated fixture is built from our own declarations, so a check that only regenerates it
proves the generator agrees with itself. These tests hold every committed AMC fixture to sources
the generator doesn't own: the governed query pack (through the producer's own
`validate_result`), the column registry's types and evidence statuses, and the suppression shape
observed live on 2026-10-08. Each control pairs a conforming input with a drifted one and asserts
the exact refusal, so a check that stopped firing would fail here rather than pass quietly.
"""

from __future__ import annotations

import csv
import dataclasses
import functools
import io
import pathlib
import tempfile
import warnings
from collections.abc import Callable
from datetime import date, timedelta

import pytest

from adapters.amc_csv import dict_rows
from adapters.jobs.fixture_generation.catalog import (
    SyntheticCatalogError,
    load_synthetic_entity_catalog,
)
from adapters.jobs.fixture_generation.conformance import (
    ColumnMarker,
    ConformanceDeclaration,
    ConformanceInputs,
    Finding,
    FixtureUnderTest,
    GeneratedReport,
    Sources,
    SuppressionOutput,
    check_daily,
    check_exposure_frequency,
    check_exposure_groups,
    check_generated,
    check_periodic,
    check_suppression,
    check_trap,
    inputs_from,
    run_conformance,
)
from adapters.jobs.fixture_generation.shapes import (
    FixtureBuildOptions,
    FixtureError,
    ResultShape,
    build,
)
from adapters.jobs.land_fixtures.bundle import AMC_FIXTURE_CONTRACTS
from domain.data_contracts.schema import ColumnRegistry

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-053", "ADR-051")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
GENERATED = ROOT / "tests" / "fixtures" / "generated"
RESULTS = ROOT / "validation" / "RESULTS.md"
FIXTURE = "amc-dsp-impressions.csv"
CONTRACT = "amc-dsp-impressions"


class FixtureMarkerWarning(UserWarning):
    """Raised once per applied marker or declared trap deviation, so the run shows each."""


@functools.cache
def _base() -> ConformanceInputs:
    return inputs_from(ROOT, {})


@functools.cache
def _committed() -> ConformanceInputs:
    base = _base()
    return dataclasses.replace(
        base,
        generated={
            f"{contract_id}.csv": (GENERATED / f"{contract_id}.csv").read_text()
            for contract_id in sorted(base.packs)
        },
    )


HEADER = (
    "impression_date_utc,campaign_id_string,campaign_country,impressions,total_cost,"
    "currency_iso_code,time_window_start,time_window_end,distinct_user_count,"
    "suppression_reason,rows_suppressed"
)
WINDOW = "2025-07-01T00:00:00.000Z,2026-10-07T00:00:00.000Z"
KEPT = f"2025-07-01,910000000000000001,US,2971,21607830,USD,{WINDOW},2,,false"
WITHHELD = f",,,457,139400,,{WINDOW},1,1 dsp_impressions users < 2 required,true"
"""A suppressed row as AMC delivered one live: source keys NULL, measures kept, count = reason."""

IMPRESSIONS_MARKER = ColumnMarker(
    contract_id=CONTRACT,
    column="campaign_country",
    status="pending",
    reason=(
        "Docs-verified STRING, DIMENSION, LOW (datasources/dsp_impressions, read 2026-10-08). "
        "Resolves on the STANDARD demo instance and is absent on the Sandbox (2026-10-08). Only "
        "its observed representation is pending; the fixture value 'US' is the page's example."
    ),
)


def _csv(*lines: str) -> str:
    return "".join(f"{line}\n" for line in lines)


COUNTRY = ("dsp_impressions", "campaign_country")


def _check(
    text: str,
    *,
    sources: Sources | None = None,
    declaration: ConformanceDeclaration | None = None,
) -> GeneratedReport:
    inputs = _base()
    return check_generated(
        FixtureUnderTest(FIXTURE, text, inputs.contracts[CONTRACT], inputs.packs[CONTRACT]),
        sources or _sources(),
        declaration or inputs.declaration,
    )


def _sources(
    *, registry: ColumnRegistry | None = None, pending: frozenset[tuple[str, str]] | None = None
) -> Sources:
    inputs = _base()
    return Sources(
        registry or inputs.registry,
        inputs.shape,
        inputs.pending if pending is None else pending,
    )


def _disputed_country() -> ColumnRegistry:
    """Copy the real registry with dsp_impressions.campaign_country marked disputed."""
    registry = _base().registry
    columns = dict(registry.columns)
    columns[COUNTRY] = dataclasses.replace(columns[COUNTRY], disputed=True)
    return dataclasses.replace(registry, columns=columns)


# --- the committed fixtures ---------------------------------------------------------------------


def test_every_committed_amc_fixture_conforms_and_each_exception_is_reported() -> None:
    run = run_conformance(_committed())
    for item in (*run.markers, *run.trap_deviations):
        warnings.warn(str(item), FixtureMarkerWarning, stacklevel=1)
        print(item)  # noqa: T201 - shown with -s or -rA; the warning shows it in every run

    assert run.findings == ()
    assert tuple((m.contract_id, m.column, m.status) for m in run.markers) == (
        ("amc-audience-performance", "campaign_country", "pending"),
        ("amc-campaign-dsp", "campaign_country", "pending"),
        ("amc-dsp-clicks", "campaign_country", "pending"),
        ("amc-dsp-impressions", "campaign_country", "pending"),
        ("amc-dsp-reach", "campaign_country", "pending"),
        ("amc-dsp-segment-names", "campaign_country", "pending"),
        ("amc-dsp-views", "campaign_country", "pending"),
    )
    assert tuple(d.fixture for d in run.trap_deviations) == (
        "traps/amc-suppression-merged.csv",
        "traps/amc-withheld-residual.csv",
        "traps/amc-dsp-partial-overlap-impressions.csv",
        "traps/amc-dsp-partial-overlap-clicks.csv",
        "traps/amc-dsp-partial-overlap-views.csv",
        "traps/amc-attributed-conversion-governed.csv",
    )


def test_the_committed_impressions_fixture_opens_with_the_rows_these_controls_use() -> None:
    lines = (GENERATED / FIXTURE).read_text().splitlines()
    assert lines[:2] == [HEADER, KEPT]


def test_the_registry_shape_is_the_one_observed_live_on_2026_10_08() -> None:
    """Pinned here as well as in the registry, so drifting either copy fails.

    The values are what `validation/RESULTS.md` § "Paid-feature presence and forced suppression
    — 2026-10-08" records, on both instance types, controls on.
    """
    shape = _base().shape
    assert shape == SuppressionOutput(
        observed="2026-10-08",
        source="validation/RESULTS.md#paid-feature-presence-and-forced-suppression--2026-10-08",
        trailing_order=("distinct_user_count", "suppression_reason", "rows_suppressed"),
        discriminator_values=("true", "false"),
        suppressed_reason_format="{users} {table} users < {bar} required",
        timestamp_format="%Y-%m-%dT%H:%M:%S.000Z",
        bars={"NONE": 1, "LOW": 2, "MEDIUM": 100, "HIGH": 100, "VERY_HIGH": None, "INTERNAL": None},
    )
    assert "\n## Paid-feature presence and forced suppression — 2026-10-08\n" in (
        RESULTS.read_text()
    )


def test_each_landed_amc_trap_is_declared_for_the_contract_it_lands_as() -> None:
    declared = {d.fixture: d.contract_id for d in _base().declaration.trap_deviations}
    landed = {name: cid for name, cid in AMC_FIXTURE_CONTRACTS.items() if name.startswith("traps/")}
    assert {name: declared[name] for name in landed} == landed


# --- the paired controls on one generated fixture --------------------------------------------


def test_a_live_shaped_slice_conforms_with_its_marker_reported() -> None:
    assert _check(_csv(HEADER, KEPT, WITHHELD)) == GeneratedReport(
        findings=(), markers=(IMPRESSIONS_MARKER,)
    )


def test_the_pre_2026_10_08_layout_is_refused_by_the_pack_and_the_live_shape() -> None:
    """The layout every AMC fixture had before this check: no window, request order, reach count."""
    old = _csv(
        "impression_date_utc,campaign_id_string,campaign_country,impressions,total_cost,"
        "currency_iso_code,rows_suppressed,suppression_reason,distinct_user_count",
        "2025-07-01,910000000000000001,US,2971,21607830,USD,false,,201",
    )
    assert _check(old).findings == (
        Finding(
            "RESULT_COLUMNS_MISSING",
            FIXTURE,
            "result is missing time_window_end, time_window_start.",
        ),
        Finding(
            "RESULT_WINDOW_START_INCONSISTENT",
            FIXTURE,
            "every result row must carry one nonblank time_window_start.",
        ),
        Finding(
            "RESULT_WINDOW_END_INCONSISTENT",
            FIXTURE,
            "every result row must carry one nonblank time_window_end.",
        ),
        Finding(
            "FIXTURE_TRAILING_ORDER",
            FIXTURE,
            "the last 3 columns are ['rows_suppressed', 'suppression_reason', "
            "'distinct_user_count']; AMC delivers ['distinct_user_count', 'suppression_reason', "
            "'rows_suppressed'].",
        ),
        Finding(
            "FIXTURE_UNSUPPRESSED_USER_COUNT",
            FIXTURE,
            "1 row(s), first at data row 1: distinct_user_count='201'.",
        ),
    )


def test_an_unknown_column_is_refused_by_the_pack_and_the_contract() -> None:
    header = HEADER.replace("currency_iso_code,", "currency_iso_code,winning_bid_cost,")
    row = KEPT.replace(",USD,", ",USD,12345,")
    assert _check(_csv(header, row)).findings == (
        Finding("RESULT_COLUMNS_UNEXPECTED", FIXTURE, "result adds winning_bid_cost."),
        Finding(
            "FIXTURE_COLUMN_UNMAPPED",
            FIXTURE,
            "winning_bid_cost is not delivered by any field of amc-dsp-impressions.",
        ),
    )


def test_a_column_the_registry_does_not_list_is_refused() -> None:
    registry = _base().registry
    columns = {key: value for key, value in registry.columns.items() if key != COUNTRY}
    report = _check(
        _csv(HEADER, KEPT),
        sources=_sources(registry=dataclasses.replace(registry, columns=columns)),
    )
    assert report == GeneratedReport(
        findings=(
            Finding(
                "FIXTURE_COLUMN_UNREGISTERED",
                FIXTURE,
                "campaign_country is not a registered column of dsp_impressions.",
            ),
        ),
        markers=(),
    )


def test_a_value_outside_the_registry_type_is_refused() -> None:
    row = KEPT.replace(",2971,", ",2971.5,")
    assert _check(_csv(HEADER, row)).findings == (
        Finding(
            "FIXTURE_TYPE_MISMATCH",
            FIXTURE,
            "impressions is LONG; 1 row(s), first at data row 1: '2971.5'.",
        ),
    )


# --- registry status: pending and disputed columns need an explicit marker ----------------------


def test_a_pending_column_without_a_marker_is_refused() -> None:
    report = _check(
        _csv(HEADER, KEPT),
        declaration=dataclasses.replace(_base().declaration, markers=()),
    )
    assert report == GeneratedReport(
        findings=(
            Finding(
                "FIXTURE_COLUMN_PENDING_UNMARKED",
                FIXTURE,
                "campaign_country is pending on dsp_impressions and no marker allows it.",
            ),
        ),
        markers=(),
    )


def test_a_disputed_column_is_refused_under_a_pending_marker() -> None:
    report = _check(_csv(HEADER, KEPT), sources=_sources(registry=_disputed_country()))
    assert report == GeneratedReport(
        findings=(
            Finding(
                "FIXTURE_COLUMN_DISPUTED_UNMARKED",
                FIXTURE,
                "campaign_country is disputed on dsp_impressions and no marker allows it.",
            ),
        ),
        markers=(),
    )


def test_a_disputed_column_passes_only_under_a_disputed_marker_which_is_reported() -> None:
    marker = dataclasses.replace(IMPRESSIONS_MARKER, status="disputed", reason="test marker")
    report = _check(
        _csv(HEADER, KEPT),
        sources=_sources(registry=_disputed_country()),
        declaration=dataclasses.replace(_base().declaration, markers=(marker,)),
    )
    assert report == GeneratedReport(findings=(), markers=(marker,))


def test_a_marker_the_registry_no_longer_supports_is_refused() -> None:
    report = _check(_csv(HEADER, KEPT), sources=_sources(pending=_base().pending - {COUNTRY}))
    assert report == GeneratedReport(
        findings=(
            Finding(
                "FIXTURE_MARKER_STALE",
                FIXTURE,
                "campaign_country is marked pending but the registry records no pending claim "
                "on dsp_impressions.",
            ),
        ),
        markers=(),
    )


# --- suppression markers against the live shape -----------------------------------------------


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (
            WITHHELD.replace("1 dsp_impressions", "1 sandbox.dsp_impressions"),
            Finding(
                "FIXTURE_SUPPRESSED_REASON",
                FIXTURE,
                "1 row(s), first at data row 2: "
                "suppression_reason='1 sandbox.dsp_impressions users < 2 required'.",
            ),
        ),
        (
            WITHHELD.replace("1 dsp_impressions", "1 'dsp_impressions'"),
            Finding(
                "FIXTURE_SUPPRESSED_REASON",
                FIXTURE,
                "1 row(s), first at data row 2: "
                "suppression_reason=\"1 'dsp_impressions' users < 2 required\".",
            ),
        ),
        (
            WITHHELD.replace(",139400,,", ",139400,USD,"),
            Finding(
                "FIXTURE_SUPPRESSED_KEY_PRESENT",
                FIXTURE,
                "1 row(s), first at data row 2: kept ['currency_iso_code'].",
            ),
        ),
        (
            KEPT.replace(",,false", ",withheld,false"),
            Finding(
                "FIXTURE_UNSUPPRESSED_REASON",
                FIXTURE,
                "1 row(s), first at data row 2: suppression_reason='withheld'.",
            ),
        ),
        (
            KEPT.replace(",2,,false", ",201,,false"),
            Finding(
                "FIXTURE_UNSUPPRESSED_USER_COUNT",
                FIXTURE,
                "1 row(s), first at data row 2: distinct_user_count='201'.",
            ),
        ),
        (
            WITHHELD.replace(",1,1 dsp", ",,1 dsp"),
            Finding(
                "FIXTURE_SUPPRESSED_USER_COUNT",
                FIXTURE,
                "1 row(s), first at data row 2: distinct_user_count=''.",
            ),
        ),
        (
            WITHHELD.replace(",true", ",TRUE"),
            Finding(
                "FIXTURE_DISCRIMINATOR_VALUE",
                FIXTURE,
                "1 row(s), first at data row 2: rows_suppressed='TRUE'.",
            ),
        ),
    ],
    ids=[
        "sandbox-prefix",
        "documented-quoted-format",
        "key-kept",
        "reason-on-kept-row",
        "reach-as-user-count",
        "blank-count",
        "discriminator-case",
    ],
)
def test_each_suppression_marker_drift_is_refused_exactly(row: str, expected: Finding) -> None:
    assert _check(_csv(HEADER, KEPT, row)).findings == (expected,)


def test_the_live_shape_lets_measures_and_derived_labels_survive_suppression() -> None:
    """`total_cost` and `impressions` are NONE (bar 1), so a one-user suppressed row keeps them."""
    inputs = _base()
    report = check_suppression(
        FixtureUnderTest(
            FIXTURE, _csv(HEADER, WITHHELD), inputs.contracts[CONTRACT], inputs.packs[CONTRACT]
        ),
        _sources(),
    )
    assert report == ()


# --- AMC's CSV escape: a backslash-escaped quote, never RFC 4180's doubling ----------------------

CAMPAIGN_DSP = "amc-campaign-dsp"


def _campaign_check(text: str) -> GeneratedReport:
    inputs = _base()
    return check_generated(
        FixtureUnderTest(
            f"{CAMPAIGN_DSP}.csv", text, inputs.contracts[CAMPAIGN_DSP], inputs.packs[CAMPAIGN_DSP]
        ),
        _sources(),
        inputs.declaration,
    )


def test_a_backslash_escaped_name_conforms_and_its_rfc_4180_doubling_is_refused() -> None:
    lines = (GENERATED / f"{CAMPAIGN_DSP}.csv").read_text().splitlines()
    quoted = next(line for line in lines if '"Northstar DSP | \\"Flight\\" 27"' in line)
    # Positive control: the generator's backslash form is the live one and conforms.
    assert _campaign_check(_csv(lines[0], quoted)).findings == ()
    doubled = quoted.replace('\\"', '""')
    assert _campaign_check(_csv(lines[0], doubled)) == GeneratedReport(
        findings=(
            Finding(
                "CSV_ESCAPE_AMBIGUOUS",
                f"{CAMPAIGN_DSP}.csv",
                "line 2, column campaign: a quoted field holds a doubled quote, which is RFC "
                "4180's escape and not AMC's backslash.",
            ),
        ),
        markers=(),
    )


# --- traps: declared differences only -----------------------------------------------------------


def test_a_trap_whose_differences_change_is_refused() -> None:
    """The clicks trap is live-shaped and declares no difference; its old request order drifts."""
    inputs = _base()
    name = "traps/amc-dsp-partial-overlap-clicks.csv"
    original = inputs.traps[name]
    header, *rows = original.splitlines()
    request_order = header.replace(
        "distinct_user_count,suppression_reason,rows_suppressed",
        "rows_suppressed,suppression_reason,distinct_user_count",
    )
    reordered = [
        ",".join([*cells[:-3], cells[-1], cells[-2], cells[-3]])
        for cells in (line.split(",") for line in rows)
    ]
    # Positive control: the committed trap matches its empty declaration.
    assert (
        check_trap(
            FixtureUnderTest(
                name, original, inputs.contracts["amc-dsp-clicks"], inputs.packs["amc-dsp-clicks"]
            ),
            _sources(),
            next(item for item in inputs.declaration.trap_deviations if item.fixture == name),
        )
        == ()
    )
    drifted = dataclasses.replace(
        inputs, traps={name: _csv(request_order, *reordered)}, generated={}
    )
    run = run_conformance(drifted)
    assert run.findings == (
        *(
            Finding(
                "FIXTURE_MARKER_STALE",
                contract_id,
                "campaign_country is marked pending but no generated fixture is checked "
                "for that contract.",
            )
            for contract_id in (
                "amc-dsp-impressions",
                "amc-campaign-dsp",
                "amc-dsp-reach",
                "amc-dsp-segment-names",
                "amc-audience-performance",
                "amc-dsp-clicks",
                "amc-dsp-views",
            )
        ),
        Finding("TRAP_DEVIATION_DRIFT", name, "declared [], found ['FIXTURE_TRAILING_ORDER']."),
        *(
            Finding("TRAP_DECLARATION_ORPHANED", fixture, "no AMC trap of that name was read.")
            for fixture in (
                "traps/amc-attributed-conversion-governed.csv",
                "traps/amc-dsp-partial-overlap-impressions.csv",
                "traps/amc-dsp-partial-overlap-views.csv",
                "traps/amc-suppression-merged.csv",
                "traps/amc-withheld-residual.csv",
            )
        ),
    )


def test_an_undeclared_amc_trap_is_refused() -> None:
    inputs = _committed()
    name = "traps/amc-new-trap.csv"
    run = run_conformance(
        dataclasses.replace(inputs, traps={**inputs.traps, name: _csv(HEADER, KEPT)})
    )
    assert run.findings == (
        Finding(
            "TRAP_UNDECLARED",
            name,
            "an AMC trap needs a trap_deviations entry, even one with no codes.",
        ),
    )


# --- the generator refuses a pack it cannot honour ----------------------------------------------


def test_the_generator_refuses_a_pack_whose_columns_it_does_not_produce() -> None:
    inputs = _base()
    pack = inputs.packs[CONTRACT]
    shape = ResultShape(
        columns=(*pack.expected_columns, "city_name"),
        expected_bar=pack.expected_bar,
        trailing_order=inputs.shape.trailing_order,
        timestamp_format=inputs.shape.timestamp_format,
    )
    with pytest.raises(FixtureError) as caught:
        build(inputs.contracts[CONTRACT], inputs.registry, FixtureBuildOptions(result=shape))
    assert str(caught.value) == (
        "amc-dsp-impressions: the pack's outputColumns disagree with the generated columns; "
        "missing=['city_name'], extra=[]."
    )


# --- the periodic pack: one delivery per complete period (ADR-100, FEAT-003) ---------------------

PERIODIC = "amc-new-to-brand-buyers-platform"
PERIODIC_HEADER = (
    "conversion_event_marketplace_name,buyers_new_to_brand_12mo,buyers_attributed,"
    "time_window_start,time_window_end,distinct_user_count,suppression_reason,rows_suppressed"
)
PERIODIC_WINDOW = "2026-09-14T00:00:00.000Z,2026-09-21T00:00:00.000Z"
PERIODIC_SUPPRESSED = (
    f",1,1,{PERIODIC_WINDOW},1,"
    "1 amazon_attributed_events_by_conversion_time users < 2 required,true"
)


def _periodic(text: str) -> FixtureUnderTest:
    inputs = _base()
    return FixtureUnderTest(
        f"{PERIODIC}.csv", text, inputs.contracts[PERIODIC], inputs.packs[PERIODIC]
    )


def _corpus() -> str:
    return (GENERATED / "amc-attributed-conversion.csv").read_text()


def _corpus_purchases(start: str, end: str) -> tuple[int, int]:
    rows = tuple(csv.DictReader(io.StringIO(_corpus())))
    inside = tuple(row for row in rows if start <= row["conversion_event_date_utc"] <= end)
    return (
        sum(int(row["new_to_brand_purchases"]) for row in inside),
        sum(int(row["purchases"]) for row in inside),
    )


def test_a_periodic_week_within_its_corpus_bound_conforms() -> None:
    ntb, purchases = _corpus_purchases("2026-09-14", "2026-09-20")
    kept = f"AMAZON.COM,{ntb},{purchases},{PERIODIC_WINDOW},2,,false"
    text = _csv(PERIODIC_HEADER, kept, PERIODIC_SUPPRESSED)
    assert check_periodic(_periodic(text), _corpus()) == ()
    assert check_suppression(_periodic(text), _sources()) == ()
    assert check_generated(_periodic(text), _sources(), _base().declaration).findings == ()


def test_a_window_misaligned_to_the_declared_grain_is_refused() -> None:
    shifted = "2026-09-15T00:00:00.000Z,2026-09-22T00:00:00.000Z"
    text = _csv(PERIODIC_HEADER, f"AMAZON.COM,1,2,{shifted},2,,false")
    assert check_periodic(_periodic(text), _corpus()) == (
        Finding(
            "FIXTURE_PERIOD_WINDOW_MISALIGNED",
            f"{PERIODIC}.csv",
            f"1 row(s), first at data row 1: window {shifted}.",
        ),
    )


def test_a_kept_key_on_a_suppressed_periodic_row_is_refused() -> None:
    kept_key = PERIODIC_SUPPRESSED.replace(",1,1,", "AMAZON.COM,1,1,", 1)
    assert check_suppression(_periodic(_csv(PERIODIC_HEADER, kept_key)), _sources()) == (
        Finding(
            "FIXTURE_SUPPRESSED_KEY_PRESENT",
            f"{PERIODIC}.csv",
            "1 row(s), first at data row 1: kept ['conversion_event_marketplace_name'].",
        ),
    )


def test_buyers_above_the_period_corpus_purchases_are_refused() -> None:
    ntb, purchases = _corpus_purchases("2026-09-14", "2026-09-20")
    drifted = f"AMAZON.COM,{ntb + 1},{purchases},{PERIODIC_WINDOW},2,,false"
    assert check_periodic(_periodic(_csv(PERIODIC_HEADER, drifted)), _corpus()) == (
        Finding(
            "FIXTURE_BUYERS_EXCEED_PURCHASES",
            f"{PERIODIC}.csv",
            f"1 row(s), first at data row 1: buyers_new_to_brand_12mo={ntb + 1} > "
            f"new_to_brand_purchases={ntb}.",
        ),
    )
    # New-to-brand buyers are a subset of attributed buyers.
    swapped = f"AMAZON.COM,{ntb},{ntb - 1},{PERIODIC_WINDOW},2,,false"
    assert check_periodic(_periodic(_csv(PERIODIC_HEADER, swapped)), _corpus()) == (
        Finding(
            "FIXTURE_BUYERS_EXCEED_PURCHASES",
            f"{PERIODIC}.csv",
            f"1 row(s), first at data row 1: buyers_new_to_brand_12mo={ntb} > "
            f"buyers_attributed={ntb - 1}.",
        ),
    )


def test_each_periodic_delivery_is_checked_on_its_own_window() -> None:
    """Many windows in one file are many deliveries, not one inconsistent result."""
    month = "2026-09-01T00:00:00.000Z,2026-10-01T00:00:00.000Z"
    text = _csv(
        PERIODIC_HEADER,
        f"AMAZON.COM,1,2,{PERIODIC_WINDOW},2,,false",
        f"AMAZON.COM,1,2,{month},2,,false",
    )
    assert check_generated(_periodic(text), _sources(), _base().declaration).findings == ()
    # Positive control: the same two windows on a daily pack's fixture are inconsistent.
    daily = _check(
        _csv(HEADER, KEPT, KEPT.replace("2026-10-07T00:00:00.000Z", "2026-10-08T00:00:00.000Z"))
    )
    assert (
        Finding(
            "RESULT_WINDOW_END_INCONSISTENT",
            FIXTURE,
            "every result row must carry one nonblank time_window_end.",
        )
        in daily.findings
    )


# --- pack 2: one row per ad product per period, bounded by its purchases and pack 1 ------------

CHANNEL = "amc-new-to-brand-buyers-channel"
CHANNEL_HEADER = (
    "conversion_event_marketplace_name,ad_product_type,buyers_new_to_brand_12mo,buyers_attributed,"
    "time_window_start,time_window_end,distinct_user_count,suppression_reason,rows_suppressed"
)


def _channel(text: str) -> FixtureUnderTest:
    inputs = _base()
    return FixtureUnderTest(
        f"{CHANNEL}.csv", text, inputs.contracts[CHANNEL], inputs.packs[CHANNEL]
    )


def _ad_product_purchases(ad_product: str) -> tuple[int, int]:
    rows = tuple(csv.DictReader(io.StringIO(_corpus())))
    inside = tuple(
        row
        for row in rows
        if "2026-09-14" <= row["conversion_event_date_utc"] <= "2026-09-20"
        and row["ad_product_type"] == ad_product
    )
    return (
        sum(int(row["new_to_brand_purchases"]) for row in inside),
        sum(int(row["purchases"]) for row in inside),
    )


def _platform(ntb: int, buyers: int) -> str:
    return _csv(PERIODIC_HEADER, f"AMAZON.COM,{ntb},{buyers},{PERIODIC_WINDOW},2,,false")


def test_a_per_ad_product_row_within_its_purchases_and_the_platform_row_conforms() -> None:
    ntb, purchases = _ad_product_purchases("sponsored_brands")
    kept = f"AMAZON.COM,sponsored_brands,{ntb},{purchases},{PERIODIC_WINDOW},2,,false"
    text = _csv(CHANNEL_HEADER, kept, PERIODIC_SUPPRESSED.replace(",1,1,", ",,1,1,", 1))
    assert check_periodic(_channel(text), _corpus(), _platform(ntb, purchases)) == ()
    assert check_generated(_channel(text), _sources(), _base().declaration).findings == ()


def test_a_per_ad_product_row_above_its_own_ad_product_s_purchases_is_refused() -> None:
    """Bounded by its own ad product's purchases, never the whole period's (control above)."""
    ntb, purchases = _ad_product_purchases("sponsored_brands")
    drifted = f"AMAZON.COM,sponsored_brands,{ntb + 1},{purchases},{PERIODIC_WINDOW},2,,false"
    assert check_periodic(_channel(_csv(CHANNEL_HEADER, drifted)), _corpus()) == (
        Finding(
            "FIXTURE_BUYERS_EXCEED_PURCHASES",
            f"{CHANNEL}.csv",
            f"1 row(s), first at data row 1: buyers_new_to_brand_12mo={ntb + 1} > "
            f"new_to_brand_purchases={ntb}.",
        ),
    )


def test_a_per_ad_product_row_above_the_same_period_s_platform_row_is_refused() -> None:
    ntb, purchases = _ad_product_purchases("sponsored_brands")
    kept = f"AMAZON.COM,sponsored_brands,{ntb},{purchases},{PERIODIC_WINDOW},2,,false"
    platform = _platform(ntb - 1, purchases)
    assert check_periodic(_channel(_csv(CHANNEL_HEADER, kept)), _corpus(), platform) == (
        Finding(
            "FIXTURE_BUYERS_EXCEED_PLATFORM",
            f"{CHANNEL}.csv",
            f"1 row(s), first at data row 1: buyers_new_to_brand_12mo={ntb} > platform "
            f"buyers_new_to_brand_12mo={ntb - 1}.",
        ),
    )


def test_a_kept_per_ad_product_row_under_a_suppressed_platform_row_is_refused() -> None:
    """A suppressed platform row bounds nothing, so the gap is a finding, never a skipped check."""
    ntb, purchases = _ad_product_purchases("sponsored_brands")
    kept = f"AMAZON.COM,sponsored_brands,{ntb},{purchases},{PERIODIC_WINDOW},2,,false"
    suppressed = _csv(PERIODIC_HEADER, PERIODIC_SUPPRESSED)
    assert check_periodic(_channel(_csv(CHANNEL_HEADER, kept)), _corpus(), suppressed) == (
        Finding(
            "FIXTURE_BUYERS_EXCEED_PLATFORM",
            f"{CHANNEL}.csv",
            "1 row(s), first at data row 1: no kept platform row for its period and marketplace.",
        ),
    )
    # Paired: the same row under a kept platform row conforms.
    assert (
        check_periodic(_channel(_csv(CHANNEL_HEADER, kept)), _corpus(), _platform(ntb, purchases))
        == ()
    )


# --- pack 3: one row per campaign per calendar month, bounded by its purchases and pack 1 -------

CAMPAIGN = "amc-new-to-brand-buyers-campaign"
CAMPAIGN_HEADER = (
    "conversion_event_marketplace_name,ad_product_type,campaign_id_string,"
    "buyers_new_to_brand_12mo,buyers_attributed,time_window_start,time_window_end,"
    "distinct_user_count,suppression_reason,rows_suppressed"
)
MONTH_WINDOW = "2026-09-01T00:00:00.000Z,2026-10-01T00:00:00.000Z"
CAMPAIGN_ID = "910000000000000003"


def _campaign(text: str) -> FixtureUnderTest:
    inputs = _base()
    return FixtureUnderTest(
        f"{CAMPAIGN}.csv", text, inputs.contracts[CAMPAIGN], inputs.packs[CAMPAIGN]
    )


def _campaign_purchases(campaign: str) -> tuple[int, int]:
    rows = tuple(csv.DictReader(io.StringIO(_corpus())))
    inside = tuple(
        row
        for row in rows
        if "2026-09-01" <= row["conversion_event_date_utc"] <= "2026-09-30"
        and row["campaign_id_string"] == campaign
    )
    return (
        sum(int(row["new_to_brand_purchases"]) for row in inside),
        sum(int(row["purchases"]) for row in inside),
    )


def _platform_month(ntb: int, buyers: int) -> str:
    return _csv(PERIODIC_HEADER, f"AMAZON.COM,{ntb},{buyers},{MONTH_WINDOW},2,,false")


def test_a_per_campaign_row_within_its_purchases_and_the_platform_month_conforms() -> None:
    ntb, purchases = _campaign_purchases(CAMPAIGN_ID)
    kept = f"AMAZON.COM,amazon_dsp,{CAMPAIGN_ID},{ntb},{purchases},{MONTH_WINDOW},2,,false"
    suppressed = (
        f",,,1,1,{MONTH_WINDOW},1,"
        "1 amazon_attributed_events_by_conversion_time users < 2 required,true"
    )
    text = _csv(CAMPAIGN_HEADER, kept, suppressed)
    assert check_periodic(_campaign(text), _corpus(), _platform_month(ntb, purchases)) == ()
    assert check_generated(_campaign(text), _sources(), _base().declaration).findings == ()


def test_a_per_campaign_row_above_its_own_campaign_s_purchases_is_refused() -> None:
    """Bounded by its own campaign's purchases in the month, never the whole month's."""
    ntb, purchases = _campaign_purchases(CAMPAIGN_ID)
    drifted = f"AMAZON.COM,amazon_dsp,{CAMPAIGN_ID},{ntb + 1},{purchases},{MONTH_WINDOW},2,,false"
    assert check_periodic(_campaign(_csv(CAMPAIGN_HEADER, drifted)), _corpus()) == (
        Finding(
            "FIXTURE_BUYERS_EXCEED_PURCHASES",
            f"{CAMPAIGN}.csv",
            f"1 row(s), first at data row 1: buyers_new_to_brand_12mo={ntb + 1} > "
            f"new_to_brand_purchases={ntb}.",
        ),
    )


def test_a_per_campaign_week_is_refused_as_misaligned_to_its_month_grain() -> None:
    """Pack 3 declares calendar months only (control: the month row above conforms)."""
    week = f"AMAZON.COM,amazon_dsp,{CAMPAIGN_ID},1,2,{PERIODIC_WINDOW},2,,false"
    findings = check_periodic(_campaign(_csv(CAMPAIGN_HEADER, week)), _corpus())
    assert [item.code for item in findings] == ["FIXTURE_PERIOD_WINDOW_MISALIGNED"]


# --- pack 4: one delivery per UTC day, bounded by the same day's impressions -------------------

REACH = "amc-dsp-reach"
REACH_HEADER = (
    "impression_date_utc,campaign_id_string,campaign_country,reach_users,time_window_start,"
    "time_window_end,distinct_user_count,suppression_reason,rows_suppressed"
)


def _reach(text: str) -> FixtureUnderTest:
    inputs = _base()
    return FixtureUnderTest(f"{REACH}.csv", text, inputs.contracts[REACH], inputs.packs[REACH])


def _impressions_on(day: str, campaign: str) -> int:
    rows = tuple(csv.DictReader(io.StringIO((GENERATED / FIXTURE).read_text())))
    return sum(
        int(row["impressions"])
        for row in rows
        if row["impression_date_utc"] == day and row["campaign_id_string"] == campaign
    )


def _reach_row(day: str, campaign: str, reach: int) -> str:
    return (
        f"{day},{campaign},US,{reach},{day}T00:00:00.000Z,"
        f"{date.fromisoformat(day) + timedelta(days=1)}T00:00:00.000Z,2,,false"
    )


def test_reach_within_the_same_day_s_impressions_conforms_and_above_is_refused() -> None:
    day, campaign = "2026-10-04", "910000000000000001"
    ceiling = _impressions_on(day, campaign)
    impressions = (GENERATED / FIXTURE).read_text()
    assert (
        check_daily(_reach(_csv(REACH_HEADER, _reach_row(day, campaign, ceiling))), impressions)
        == ()
    )
    drifted = _csv(REACH_HEADER, _reach_row(day, campaign, ceiling + 1))
    assert check_daily(_reach(drifted), impressions) == (
        Finding(
            "FIXTURE_DISTINCT_EXCEEDS_CORPUS",
            f"{REACH}.csv",
            f"1 row(s), first at data row 1: reach_users={ceiling + 1} > impressions={ceiling}.",
        ),
    )


def test_a_reach_window_wider_than_its_day_is_refused() -> None:
    row = _reach_row("2026-10-04", "910000000000000001", 1).replace(
        "2026-10-05T00:00:00.000Z", "2026-10-06T00:00:00.000Z"
    )
    assert check_daily(_reach(_csv(REACH_HEADER, row)), None) == (
        Finding(
            "FIXTURE_DAY_WINDOW_MISALIGNED",
            f"{REACH}.csv",
            "1 row(s), first at data row 1: window "
            "2026-10-04T00:00:00.000Z,2026-10-06T00:00:00.000Z.",
        ),
    )


def test_only_the_declared_day_s_delivery_is_refused() -> None:
    """The committed fixture refuses 2026-10-03 alone; the generator's declaration says so."""
    committed = _committed()
    assert committed.refused_days == {REACH: frozenset({date(2026, 10, 3)})}
    text = committed.generated[f"{REACH}.csv"]
    lines = text.splitlines()
    assert [line for line in lines if line.endswith(",true")] == [
        ",,,1,2026-10-03T00:00:00.000Z,2026-10-04T00:00:00.000Z,1,"
        "1 dsp_impressions users < 2 required,true"
    ]
    assert run_conformance(committed).findings == ()
    # Drifted: without its suppressed row the declared day isn't refused.
    clean = _csv(*(line for line in lines if not line.endswith(",true")))
    assert run_conformance(
        dataclasses.replace(committed, generated={**committed.generated, f"{REACH}.csv": clean})
    ).findings == (
        Finding(
            "FIXTURE_REFUSAL_MISSING",
            f"{REACH}.csv",
            "the delivery for 2026-10-03 is declared refused, and no suppressed row refuses it.",
        ),
    )
    # Drifted: a suppressed row on an undeclared day refuses that delivery too.
    moved = text.replace(
        "2026-10-03T00:00:00.000Z,2026-10-04T00:00:00.000Z,1,",
        "2026-10-04T00:00:00.000Z,2026-10-05T00:00:00.000Z,1,",
    )
    findings = run_conformance(
        dataclasses.replace(committed, generated={**committed.generated, f"{REACH}.csv": moved})
    ).findings
    assert findings == (
        Finding(
            "RESULT_SUPPRESSION_REFUSED",
            f"{REACH}.csv",
            "pack policy refuses any result containing a suppressed row.",
        ),
        Finding(
            "FIXTURE_REFUSAL_MISSING",
            f"{REACH}.csv",
            "the delivery for 2026-10-03 is declared refused, and no suppressed row refuses it.",
        ),
    )


# --- pack 5, amc-exposure-groups ---------------------------------------------------------------

EXPOSURE = "amc-exposure-groups"


def _exposure(text: str, refused: frozenset[date] | None = None) -> FixtureUnderTest:
    inputs = _base()
    return FixtureUnderTest(
        f"{EXPOSURE}.csv",
        text,
        inputs.contracts[EXPOSURE],
        inputs.packs[EXPOSURE],
        refused_periods=inputs.refused_periods[EXPOSURE] if refused is None else refused,
    )


def test_the_exposure_groups_fixture_conforms_and_its_drifted_copies_are_refused() -> None:
    text = (GENERATED / f"{EXPOSURE}.csv").read_text()
    assert check_exposure_groups(_exposure(text)) == ()
    # Drift: the disagreeing delivery isn't declared refused, so the catalog lies about it.
    assert {item.code for item in check_exposure_groups(_exposure(text, frozenset()))} == {
        "FIXTURE_CURRENCY_DISAGREEMENT"
    }
    # Drift: a declared refusal with no disagreement behind it.
    (declared,) = _base().refused_periods[EXPOSURE]
    extra = frozenset({declared, declared - timedelta(days=7)})
    assert {item.code for item in check_exposure_groups(_exposure(text, extra))} == {
        "FIXTURE_REFUSAL_MISSING"
    }
    # Drift: a kept row with more purchasers than shoppers.
    rows = list(csv.DictReader(io.StringIO(text)))
    kept = next(row for row in rows if row["rows_suppressed"] == "false")
    kept["purchasers"] = str(int(kept["shoppers_reached"]) + 1)
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    assert {item.code for item in check_exposure_groups(_exposure(out.getvalue()))} == {
        "FIXTURE_EXPOSURE_COUNTS_INCONSISTENT"
    }


# --- packs 6 and 7, amc-exposure-frequency-* ------------------------------------------------------

FREQUENCY = ("amc-exposure-frequency-platform", "amc-exposure-frequency-channel")


def _frequency(
    contract: str, text: str, refused: frozenset[date] | None = None
) -> FixtureUnderTest:
    inputs = _base()
    return FixtureUnderTest(
        f"{contract}.csv",
        text,
        inputs.contracts[contract],
        inputs.packs[contract],
        refused_periods=inputs.refused_periods[contract] if refused is None else refused,
    )


def _rewritten(text: str, change: Callable[[list[dict[str, str]]], None]) -> str:
    rows = list(csv.DictReader(io.StringIO(text)))
    change(rows)
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


@pytest.mark.parametrize("contract", FREQUENCY)
def test_each_frequency_fixture_conforms_and_its_drifted_copies_are_refused(contract: str) -> None:
    text = (GENERATED / f"{contract}.csv").read_text()
    assert check_exposure_frequency(_frequency(contract, text)) == ()
    # Drift: the NULL-bucket delivery isn't declared refused, so the catalog lies about it.
    assert {
        item.code for item in check_exposure_frequency(_frequency(contract, text, frozenset()))
    } == {"FIXTURE_FREQUENCY_BUCKET_NULL"}
    # Drift: a declared refusal with no NULL bucket behind it.
    (declared,) = _base().refused_periods[contract]
    extra = frozenset({declared, date(declared.year - 1, declared.month, 1)})
    assert {item.code for item in check_exposure_frequency(_frequency(contract, text, extra))} == {
        "FIXTURE_REFUSAL_MISSING"
    }

    # Drift: a single-count bucket whose impressions aren't its shoppers times its count.
    def inflate(rows: list[dict[str, str]]) -> None:
        kept = next(
            row
            for row in rows
            if row["rows_suppressed"] == "false" and row["frequency_bucket"] == "2"
        )
        kept["impressions"] = str(int(kept["impressions"]) + 1)

    assert {
        item.code
        for item in check_exposure_frequency(_frequency(contract, _rewritten(text, inflate)))
    } == {"FIXTURE_FREQUENCY_COUNTS_INCONSISTENT"}


# --- audience segments (packs 9, 13 and 8, FEAT-008 of wave 2b) ---------------------------------

SEGMENT_NAMES = "amc-dsp-segment-names"
SEGMENT_METADATA = "amc-segment-metadata"
AUDIENCE_PERFORMANCE = "amc-audience-performance"


def _rows_of(contract_id: str) -> list[dict[str, str]]:
    """Read a fixture as the load does: AMC's backslash-escaped dialect (FEAT-002)."""
    _fields, rows = dict_rows((GENERATED / f"{contract_id}.csv").read_text())
    return [{str(key): value for key, value in row.items()} for row in rows]


def test_the_audience_fixtures_carry_their_declared_shapes() -> None:
    names, metadata, performance = (
        _rows_of(item) for item in (SEGMENT_NAMES, SEGMENT_METADATA, AUDIENCE_PERFORMANCE)
    )
    # DSP and ASI segment 41001 collide and stay separate rows of separate packs.
    assert {row["behavior_segment_id"] for row in names} & {row["segment_id"] for row in metadata}
    assert {row["segment_name"] for row in names} >= {
        "Demo - Age 25-34",
        "ExperianDemographicsGenderFemale_LiveRamp",
        'Synthetic in-market: "Trail" running shoes',
    }
    # The late segment's first month comes back withheld; its name arrives the month after.
    late = [row for row in names if row["behavior_segment_id"] == "41004"]
    assert late[0]["time_window_start"] == "2025-10-01T00:00:00.000Z"
    withheld = [row for row in names if row["rows_suppressed"] == "true"]
    assert [row["time_window_start"] for row in withheld] == ["2025-09-01T00:00:00.000Z"]
    # One roster segment missing this month: September's row for Northstar Home's 41004 is
    # withheld per path, though August returned it.
    campaign = "910000000000000001"

    def returned(month: str) -> set[str]:
        return {
            row["behavior_segment_id"]
            for row in performance
            if row["time_window_start"] == month and row["campaign_id_string"] == campaign
        }

    assert "41004" in returned("2026-08-01T00:00:00.000Z")
    assert "41004" not in returned("2026-09-01T00:00:00.000Z")
    gap = [
        row
        for row in performance
        if row["rows_suppressed"] == "true" and row["time_window_start"].startswith("2026-09")
    ]
    assert [row["suppression_reason"] for row in gap] == [
        "1 'amazon_attributed_events_by_conversion_time' (path 2 of 2) users < 2 required"
    ]


def _segment_check(text: str) -> GeneratedReport:
    inputs = _base()
    return check_generated(
        FixtureUnderTest(
            f"{SEGMENT_NAMES}.csv",
            text,
            inputs.contracts[SEGMENT_NAMES],
            inputs.packs[SEGMENT_NAMES],
        ),
        _sources(),
        inputs.declaration,
    )


def test_the_segment_name_fixture_escapes_its_quote_with_a_backslash_never_doubled() -> None:
    lines = (GENERATED / f"{SEGMENT_NAMES}.csv").read_text().splitlines()
    quoted = next(line for line in lines if '\\"Trail\\"' in line)
    # Positive control: the generator's backslash form conforms.
    assert _segment_check(_csv(lines[0], quoted)).findings == ()
    doubled = quoted.replace('\\"', '""')
    assert [item.code for item in _segment_check(_csv(lines[0], doubled)).findings] == [
        "CSV_ESCAPE_AMBIGUOUS"
    ]


@pytest.mark.parametrize(
    ("drift", "message"),
    [
        ("collision", "must declare one ID in both namespaces"),
        ("quote", "must name one segment with an embedded quote"),
    ],
)
def test_a_drifted_audience_shape_is_refused(drift: str, message: str) -> None:
    path = ROOT / "contracts/shared/synthetic-entity-catalog.yaml"
    text = path.read_text()
    drifted = (
        text.replace('asi_segments: [{id: "41001"', 'asi_segments: [{id: "41901"', 1)
        if drift == "collision"
        else text.replace(
            '\\"Trail\\" running shoes", description', 'Trail running shoes", description', 1
        )
    )
    assert drifted != text
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "contracts/shared").mkdir(parents=True)
    (root / "contracts/shared/synthetic-entity-catalog.yaml").write_text(drifted)
    with pytest.raises(SyntheticCatalogError, match=message):
        load_synthetic_entity_catalog(root)
    # Positive control: the committed shape loads with both declared.
    shape = load_synthetic_entity_catalog(ROOT).shape.audience_segments
    assert {item.segment_id for item in shape.dsp_segments} & {
        item.segment_id for item in shape.asi_segments
    } == {"41001"}
