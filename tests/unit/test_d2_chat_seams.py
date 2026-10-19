# SPDX-License-Identifier: MIT-0
"""D2 chat seams from the integration sweep (d2-seam-sweep.md findings 3, 7, 8 and 9).

- F3: a question naming no period on a periodic metric reads the latest measured complete period,
  never the 7-day coverage default a daily metric uses.
- F8: every calendar preset a page declares has a governed chat reference period.
- F9: a filter on any non-string column binds a value cast to its type.
- F7: the entity-reference doc says audience segments are breakdown-only.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest
from athena_engine_support import engine_refusal
from publication_support import StubPublications

from adapters.inbound.tool_handlers import measurement
from adapters.inbound.tool_handlers.schemas import as_tool_definitions
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from adapters.outbound.athena import composition
from adapters.outbound.metric_dictionary import load_catalog
from domain.conversation.temporal import reference_periods
from domain.data_contracts.schema import CanonicalType
from domain.measurement.periodic import periodic_leaves
from domain.measurement.planning import FactShape
from domain.narrative.shaped_response import ResolutionProvenance
from domain.presentation.page_schemas import PresentationTemporalPolicy, TemporalPresetId
from domain.semantics.analytical_resolution import FieldOrigin
from domain.semantics.calendar_periods import (
    PERIODIC_GRAINS,
    latest_complete_period,
    vintage_ending,
)
from domain.semantics.time import TimeGrain
from domain.shared_kernel.errors import DomainError
from domain.shared_kernel.values import DateRange
from ports.outbound.served_periods import ServedPeriods, ServedPeriodsRequest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT)
CONTRACTS = tuple(contract for _, contract in load_contracts(ROOT))
NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
LANDED = date(2026, 10, 6)
"""The latest day every fictional contract has published through."""
PUBLISHED = (
    *(latest_complete_period(grain, LANDED) for grain in PERIODIC_GRAINS),
    vintage_ending(date(2026, 9, 1)),
)
ROLLING = DateRange(date(2026, 10, 8), date(2026, 10, 14))
"""The 7-day coverage default: never one whole period."""


@dataclass
class _Served:
    def served_periods(self, request: ServedPeriodsRequest) -> ServedPeriods:
        ids = tuple(contract.contract_id for contract in request.contracts)
        return ServedPeriods(
            frozenset((contract, period) for contract in ids for period in PUBLISHED),
            dict.fromkeys(ids, 16),
        )


def _request(metric: str, *, defaulted: bool) -> measurement.MeasurementToolRequest:
    entry = CATALOG.metric(metric)
    assert entry is not None
    selectors = {
        name: values[0] for name, values in entry.required_selectors if name != "time_grain"
    }
    origin = FieldOrigin.GOVERNED_DEFAULT if defaulted else FieldOrigin.EXPLICIT_CUSTOMER_TEXT
    return measurement.MeasurementToolRequest(
        tool=measurement.MeasurementTool.METRIC_BY_GRAIN,
        catalog=CATALOG,
        contracts=CONTRACTS,
        metric_name=metric,
        grains=("marketplace",),
        window=ROLLING,
        computed_at=NOW,
        correlation_id="corr-d2-seams",
        filters={"marketplace_key": "amazon_us", **selectors},
        resolution_provenance=(
            ResolutionProvenance("current_period", origin, "2026-10-08 to 2026-10-14"),
        ),
    )


def _served(request: measurement.MeasurementToolRequest) -> measurement.MeasurementToolRequest:
    tooling = measurement.MeasurementTooling(
        queries=None,  # type: ignore[arg-type]
        publications=StubPublications(),
        periods=_Served(),
    )
    return measurement._at_served_period(request, tooling)  # pyright: ignore[reportPrivateUsage]


def _periodic_metrics() -> list[str]:
    """Every periodic metric chat reads through the served-period path.

    The C2 share and index ratios are answered per operand (`answer_segment_profile_ratio`), so
    each of their operands is in this list instead.
    """
    names = [
        metric.name
        for metric in CATALOG.metrics
        if periodic_leaves(CATALOG, metric)
        and not measurement._is_profile_ratio(_request(metric.name, defaulted=True))  # pyright: ignore[reportPrivateUsage]
    ]
    assert names
    return names


# --- F3 ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("metric", _periodic_metrics())
def test_every_periodic_metric_resolves_a_no_period_request_to_a_measured_period(
    metric: str,
) -> None:
    answered = _served(_request(metric, defaulted=True))
    assert answered.window in {period.window for period in PUBLISHED}
    assert answered.period_note


def test_a_no_period_request_names_the_period_it_used() -> None:
    answered = _served(_request("segment_impressions", defaulted=True))
    assert answered.window == DateRange(date(2026, 9, 1), date(2026, 9, 30))
    assert answered.period_note == (
        "The question named no period, so this answer uses the latest measured month, "
        "September 2026."
    )


def test_a_named_rolling_window_is_still_refused_with_the_metrics_own_wording() -> None:
    """Paired control: only a defaulted window moves; a window the customer named is refused."""
    with pytest.raises(DomainError) as refused:
        _served(_request("segment_impressions", defaulted=False))
    description = refused.value.description
    assert "segment_impressions is measured for complete months only" in description
    # segment_impressions counts impressions, so the refusal never says distinct shoppers.
    assert "distinct shoppers" not in description


# --- the profile ratios' operands are covered --------------------------------------------------


def test_every_profile_ratio_operand_is_a_covered_periodic_metric() -> None:
    covered = set(_periodic_metrics())
    for name in ("segment_buyer_share", "segment_exposed_share", "segment_index"):
        metric = CATALOG.metric(name)
        assert metric is not None
        leaves = {leaf.name for leaf in periodic_leaves(CATALOG, metric)}
        assert leaves and leaves <= covered


# --- F8 ----------------------------------------------------------------------------------------

_PRESET_GRAINS = {
    TemporalPresetId.LAST_COMPLETE_WEEK: TimeGrain.WEEK,
    TemporalPresetId.LAST_COMPLETE_MONTH: TimeGrain.MONTH,
    TemporalPresetId.LAST_COMPLETE_QUARTER: TimeGrain.QUARTER,
}


def test_every_declared_calendar_preset_has_a_governed_chat_period() -> None:
    declared: set[TemporalPresetId] = set()
    for page in load_v2_page_schemas(ROOT).pages:
        policy = page.time_policy
        if isinstance(policy, PresentationTemporalPolicy):
            declared.update(policy.calendar_presets)
    assert TemporalPresetId.LAST_COMPLETE_QUARTER in declared
    assert declared <= set(_PRESET_GRAINS)
    # Every day across two quarter boundaries, so each preset's rollover is exercised.
    for offset in range(0, 200):
        today = date(2026, 9, 1) + timedelta(days=offset)
        windows = {period.window for period in reference_periods(today)}
        for preset in declared:
            expected = latest_complete_period(_PRESET_GRAINS[preset], today - timedelta(days=1))
            assert expected.window in windows, (today, preset)


def test_last_quarter_is_the_calendars_last_complete_quarter() -> None:
    periods = {period.label: period.window for period in reference_periods(date(2026, 10, 17))}
    assert periods["last quarter"] == DateRange(date(2026, 7, 1), date(2026, 9, 30))
    # On the first day of a quarter, the quarter that ended yesterday.
    first = {period.label: period.window for period in reference_periods(date(2027, 1, 1))}
    assert first["last quarter"] == DateRange(date(2026, 10, 1), date(2026, 12, 31))


# --- F9 ----------------------------------------------------------------------------------------

_SCHEMA = {
    table["name"]: {column["name"]: column["type"] for column in table["columns"]}
    for table in json.loads((ROOT / "contracts/generated/v2-tables.json").read_text())[
        "canonicalCurrentTables"
    ]
}
_WINDOW = {"date_key", "period_start", "period_end"}


def test_every_filterable_non_string_grain_column_composes_with_a_typed_bind() -> None:
    facts = sorted({contract.target_table for contract in CONTRACTS if contract.targets_a_fact})
    checked: list[tuple[str, str]] = []
    for fact in facts:
        shape = FactShape.for_fact(fact, CONTRACTS)
        discriminator = shape.suppression_columns[:1]
        for column in shape.grain_columns:
            kind = _SCHEMA[fact].get(column, "string")
            if kind == "string" or column in _WINDOW or column in discriminator:
                continue
            predicate = composition._equality(column, shape.typed_columns)  # pyright: ignore[reportPrivateUsage]
            assert engine_refusal(f'SELECT 1 FROM "{fact}" WHERE {predicate}') is None, predicate  # noqa: S608  # nosec B608 # A-SQL-001
            checked.append((fact, column))
    assert ("fct_shopper_acquisition", "months_since_acquisition") in checked
    assert ("fct_shopper_acquisition", "as_of_month") in checked


def test_every_canonical_non_string_type_has_a_filter_cast() -> None:
    declared = {kind.value for kind in CanonicalType if kind is not CanonicalType.STRING}
    assert declared == set(composition._CAST_TYPES)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(ValueError, match="no filter cast"):
        composition._equality("x", (("x", "array<string>"),))  # pyright: ignore[reportPrivateUsage]


# --- F7 ----------------------------------------------------------------------------------------


def test_the_entity_reference_doc_says_segments_are_breakdown_only() -> None:
    rendered = {item["name"]: item for item in as_tool_definitions()}
    for tool in ("measurement_metric_by_grain", "measurement_compare_windows", "measurement_top_n"):
        text = rendered[tool]["inputSchema"]["properties"]["entity_references"]["description"]
        assert "Audience segments are not an entity kind" in text
        assert "audience_segment grain" in text
