# SPDX-License-Identifier: MIT-0
"""Packs 14 and 15, `amc-acquisition-cohorts*`: the cohort triangle, the quarterly switch, the load.

Every delivery here is fictional. The load runs the real ingestion path (assessment, then the
sidecar) on in-memory tooling, as the packs 5 to 7 tests do; the quarterly switch is exercised over
consecutive vintages on one publication store that reads each series' own scope (plan D10).
"""

from __future__ import annotations

import json
import pathlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import cast

import pytest
from publication_support import StubPublications
from test_new_to_brand_buyers import (
    _Current,  # pyright: ignore[reportPrivateUsage]
)
from test_semantic_sidecar import (
    MemoryCanonical,
    MemoryPublications,
    _case,  # pyright: ignore[reportPrivateUsage]
)

from adapters.inbound.tool_handlers.measurement import (
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    handle,
)
from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.curation.manifests import (
    BuildManifestRequest,
    build_manifest,
    checksum_of,
    declared_columns_of,
)
from adapters.jobs.ingest_delivery.landing import LoadedDelivery
from adapters.jobs.ingest_delivery.semantic_ingestion import (
    SemanticIngestionRequest,
    ingest_semantic_delivery,
)
from adapters.jobs.ingest_delivery.semantic_sidecar import (
    SemanticSidecarOutcome,
    SemanticSidecarTooling,
)
from adapters.jobs.ingest_delivery.variant_evidence import (
    VariantEvidenceRequest,
    read_variant_evidence,
)
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import SELECTION_FAILED, PublicationSelector
from adapters.outbound.source_pack import load_source_packs
from adapters.producer.packs import (
    discard_policies,
    installed_governance,
    load_query_packs,
    pack_by_id,
)
from domain.curation.landing import GOVERNED_MANIFEST_LAYOUT, LandedObject
from domain.data_contracts.contract import CohortGrain, CohortTriangle
from domain.measurement.planning import FactShape, PlanRequest, measure_alias, plan
from domain.producer.models import PeriodValue
from domain.producer.parameters import period_parameter_value
from domain.semantics.calendar_periods import CalendarPeriod, vintage_ending
from domain.semantics.publication import PublicationScope, SuccessfulPublication
from domain.semantics.source_aligned import LandedDeliveryRef
from domain.shared_kernel.errors import DomainError, ValidationError
from domain.shared_kernel.provenance import Suppression
from domain.shopper_aggregates.cohorts import TriangleCohort, triangle_cohorts
from ports.outbound.canonical import CanonicalDestination
from ports.outbound.publication_selection import PublicationSelectionRequest
from ports.outbound.query import QueryRequest, QueryResult, QueryRow
from ports.outbound.served_periods import ServedPeriods, ServedPeriodsRequest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-099", "ADR-101", "ADR-104")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/generated"
STANDARD = "amc-acquisition-cohorts"
ARP = "amc-acquisition-cohorts-arp"
FACT = "fct_shopper_acquisition"
NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)
SETS = ("dsp", "sponsored", "dsp+sponsored")
JULY = vintage_ending(date(2026, 7, 1))
AUGUST = vintage_ending(date(2026, 8, 1))
SEPTEMBER = vintage_ending(date(2026, 9, 1))
OCTOBER = vintage_ending(date(2026, 10, 1))
_TRAILER = (
    "cohort_grain,marketplace_name,time_window_start,time_window_end,distinct_user_count,"
    "suppression_reason,rows_suppressed"
)
HEADERS = {
    STANDARD: (
        "cohort_month,months_since_acquisition,acquisition_source_set,"
        "cohort_buyers_new_to_brand_12mo,cohort_buyers_account_ids,repeat_buyers,"
        "repeat_buyers_account_ids,first_repeat_buyers,cumulative_repeat_buyers,"
        "cumulative_repeat_buyers_account_ids,repeat_purchases,repeat_revenue,purchase_currency,"
        f"purchase_currency_min,{_TRAILER}"
    ),
    ARP: (
        "cohort_month,months_since_acquisition,acquisition_source_set,"
        "cohort_buyers_account_ids_check,repeat_buyers,first_repeat_buyers,"
        "cumulative_repeat_buyers,repeat_purchases,repeat_revenue,currency_code,"
        f"currency_code_min,{_TRAILER}"
    ),
}
MONTHLY = CohortTriangle("cohort_month", "months_since_acquisition", CohortGrain.MONTH, 12)
QUARTERLY = CohortTriangle("cohort_month", "months_since_acquisition", CohortGrain.QUARTER, 3)


def _window(period: CalendarPeriod) -> str:
    end = period.end + timedelta(days=1)
    return f"{period.start.isoformat()}T00:00:00.000Z,{end.isoformat()}T00:00:00.000Z"


@dataclass(frozen=True)
class _Cell:
    cohort: str
    k: int
    source_set: str
    size: int = 400
    account_ids: int = 300
    repeat: int = 40
    revenue: str = "1200.00"
    currency: str = "USD"
    currency_min: str = "USD"
    grain: str = "month"


def _row(contract: str, cell: _Cell, period: CalendarPeriod) -> str:
    head = f"{cell.cohort},{cell.k},{cell.source_set}"
    tail = f"{cell.grain},AMAZON.COM,{_window(period)},2,,false"
    if contract == STANDARD:
        return (
            f"{head},{cell.size},{cell.account_ids},{cell.repeat},{cell.repeat - 10},"
            f"{cell.repeat // 2},{cell.repeat + cell.k},{cell.repeat + cell.k - 10},"
            f"{cell.repeat + 5},{cell.revenue},{cell.currency},{cell.currency_min},{tail}"
        )
    return (
        f"{head},{cell.account_ids},{cell.repeat},{cell.repeat // 2},{cell.repeat + cell.k},"
        f"{cell.repeat + 7},{cell.revenue},{cell.currency},{cell.currency_min},{tail}"
    )


def _cells(
    period: CalendarPeriod,
    *,
    triangle: CohortTriangle = MONTHLY,
    sets: Iterable[str] = SETS,
) -> list[_Cell]:
    grain = triangle.grain.value
    return [
        _Cell(cohort.value, k, source_set, grain=grain)
        for source_set in sets
        for cohort in triangle_cohorts(triangle, (period.start, period.end))
        for k in cohort.offsets
    ]


def _withheld(contract: str, period: CalendarPeriod, grain: str = "month") -> str:
    """Return a per-path withheld cell of three shoppers (plan D19), AMC's multi-source shape.

    Its keys, counts, repeat revenue and currency are NULL; its repeat purchases are kept.
    """
    counts = 7 if contract == STANDARD else 4
    values = [
        *([""] * 3),
        *([""] * counts),
        "3",
        "",
        "",
        "",
        grain,
        "",
        _window(period),
        "1",
        "1 'conversions' (path 2 of 2) users < 2 required",
        "true",
    ]
    return ",".join(values)


def _csv(contract: str, period: CalendarPeriod, cells: Iterable[_Cell], *extra: str) -> str:
    lines = [_row(contract, cell, period) for cell in cells]
    return "".join(f"{line}\n" for line in (HEADERS[contract], *lines, *extra))


def _delivery(  # noqa: PLR0913 -- the contract, its text, its vintage and a variant
    contract: str, text: str, period: CalendarPeriod, *, variant: str | None = None
) -> LoadedDelivery:
    body = text.encode()
    declaration = json.loads(
        (FIXTURES / "amc-new-to-brand-buyers-platform.manifest.json").read_text()
    )
    pack = pack_by_id(ROOT, contract)
    binding = pack if variant is None else pack.variant_binding(variant)
    options = {
        "period_grain": "custom",
        "as_of_month": period_parameter_value(PeriodValue.AS_OF_MONTH, (period.start, period.end)),
        **({} if variant is None else {"pack_variant": variant}),
    }
    manifest = build_manifest(
        BuildManifestRequest(
            declaration={
                **declaration,
                "option_set": options,
                "period_start": period.start.isoformat(),
                "period_end": period.end.isoformat(),
                "source_watermark": (period.end + timedelta(days=2)).isoformat(),
            },
            objects=(LandedObject("data-0001.csv", len(body), checksum_of(body)),),
            declared_columns=declared_columns_of(text, source_format="csv"),
        )
    )
    manifest = replace(
        manifest, governance=binding.governance, layout_version=GOVERNED_MANIFEST_LAYOUT
    )
    return LoadedDelivery(
        manifest=manifest,
        manifest_key=manifest.manifest_key,
        text=text,
        landed_ref=LandedDeliveryRef(
            manifest.manifest_key,
            "manifest-v1",
            "sha256:manifest",
            manifest.object_keys()[0],
            "object-v1",
            manifest.objects[0].checksum,
        ),
    )


class _ScopedPublications(MemoryPublications):
    """The memory store, reading each series' own scope (lens), as the real store does."""

    def current_for_scope(
        self, scope: PublicationScope, horizon: object = None
    ) -> tuple[SuccessfulPublication, ...]:
        del horizon
        return tuple(item for item in self.current_items.values() if item.partition.scope == scope)


@dataclass
class _Loaded:
    outcome: SemanticSidecarOutcome
    detail: str
    rows: tuple[dict[str, object], ...]
    residual: tuple[dict[str, object], ...]
    loss: dict[str, object]
    contract_rule: str


class _Store:
    """One in-memory deployment: consecutive deliveries see each other's publications."""

    def __init__(self) -> None:
        _request, tooling, order, attempts = _case()
        self.tooling: SemanticSidecarTooling = replace(
            tooling, publications=_ScopedPublications(order, attempts)
        )
        self.seen = 0

    def load(self, delivery: LoadedDelivery) -> _Loaded:
        contract_id = delivery.manifest.governance.query_pack_id  # type: ignore[union-attr]
        packs = load_query_packs(ROOT)
        results = ingest_semantic_delivery(
            SemanticIngestionRequest(
                delivery=delivery,
                # Every governed contract, as the deployed load passes them: pack 15's switch
                # reads pack 14's series (variant_follows).
                contracts=tuple(
                    contract
                    for _, contract in load_contracts(ROOT)
                    if contract.contract_id in {STANDARD, ARP}
                ),
                columns=load_registry(ROOT),
                now=NOW,
                operation_id="acquisition-cohorts-unit",
                execution_id="acquisition-cohorts-execution",
                plan_digest="sha256:acquisition-cohorts-plan",
                installed_governance=installed_governance(packs),
                discard_policies=discard_policies(packs),
            ),
            self.tooling,
        )
        (result,) = results
        canonical = self.tooling.canonical_writer
        assert isinstance(canonical, MemoryCanonical)
        fresh = canonical.requests[self.seen :]
        self.seen = len(canonical.requests)
        candidates = tuple(
            item for item in fresh if item.destination is CanonicalDestination.CANDIDATE
        )

        def rows(dataset: str) -> tuple[dict[str, object], ...]:
            return tuple(
                dict(row)
                for item in candidates
                if item.publication.partition.scope.dataset.value == dataset
                for row in item.rows
            )

        def detail(rule: str) -> str:
            return next(
                (
                    item_result.detail
                    for item in candidates
                    for item_result in item.publication.evidence.results
                    if item_result.rule_id == rule
                ),
                "",
            )

        loss = detail(f"suppression:{contract_id}.cell-loss.v1")
        return _Loaded(
            result.outcome,
            result.detail,
            rows(FACT),
            rows(f"{FACT}_withheld"),
            json.loads(loss) if loss else {},
            detail(f"contract.{contract_id}.v1"),
        )


def _load(delivery: LoadedDelivery) -> _Loaded:
    return _Store().load(delivery)


def _slices(loaded: _Loaded) -> dict[str, tuple[int, int, str]]:
    return {
        str(cast("dict[str, object]", item["slice"])["acquisition_source_set"]): (
            cast("int", item["expected_cells"]),
            cast("int", item["not_returned_cells"]),
            cast("str", item["grade"]),
        )
        for item in cast("list[dict[str, object]]", loaded.loss["slices"])
    }


# --- the triangle -------------------------------------------------------------------------------


def test_a_monthly_vintage_serves_every_m_and_k_with_m_plus_k_inside_the_as_of_month() -> None:
    cohorts = triangle_cohorts(MONTHLY, (SEPTEMBER.start, SEPTEMBER.end))
    # September 2026's vintage reads September 2025 to September 2026: twelve cohorts, the oldest
    # with all twelve follow-up months and the newest (August 2026) with one.
    assert SEPTEMBER.window.start == date(2025, 9, 1)
    assert cohorts[0] == TriangleCohort(date(2025, 9, 1), tuple(range(1, 13)))
    assert cohorts[-1] == TriangleCohort(date(2026, 8, 1), (1,))
    assert len(cohorts) == 12
    assert sum(len(item.offsets) for item in cohorts) == 78


def test_a_quarterly_vintage_keeps_quarters_wholly_inside_and_ended_follow_ups_only() -> None:
    """Retention LLD "Quarterly cohorts": Quarter 4 never fits the 13-month window."""
    at_quarter_end = triangle_cohorts(QUARTERLY, (SEPTEMBER.start, SEPTEMBER.end))
    # The vintage starts in September 2025, so Q3 2025 is only partly inside and isn't a row.
    assert [item.start for item in at_quarter_end] == [
        date(2025, 10, 1),
        date(2026, 1, 1),
        date(2026, 4, 1),
    ]
    assert [item.offsets for item in at_quarter_end] == [(1, 2, 3), (1, 2), (1,)]
    # Paired: an as-of month that is a quarter's first month (October) holds its earliest quarter
    # wholly inside, and Q3 2026's Quarter 1 hasn't ended, so Q3 2026 isn't a row yet.
    at_quarter_start = triangle_cohorts(QUARTERLY, (OCTOBER.start, OCTOBER.end))
    assert at_quarter_start[0] == TriangleCohort(date(2025, 10, 1), (1, 2, 3))
    assert date(2026, 7, 1) not in {item.start for item in at_quarter_start}
    # An as-of month inside a quarter (November): Q4 2025 starts before the window and adds no
    # expected cell.
    november = vintage_ending(date(2026, 11, 1))
    inside = triangle_cohorts(QUARTERLY, (november.start, november.end))
    assert inside[0].start == date(2026, 1, 1)
    assert max(max(item.offsets) for item in (*at_quarter_end, *at_quarter_start, *inside)) == 3


def test_the_vintage_parameter_is_the_as_of_month_in_the_probe_compiled_timestamp_form() -> None:
    assert period_parameter_value(PeriodValue.AS_OF_MONTH, (SEPTEMBER.start, SEPTEMBER.end)) == (
        "2026-09-01 00:00:00"
    )
    assert SEPTEMBER.label == "cohorts as of September 2026"
    assert SEPTEMBER.previous() == AUGUST


# --- the load -----------------------------------------------------------------------------------


def test_a_whole_monthly_vintage_publishes_every_cell_with_its_vintage_path_and_group() -> None:
    loaded = _load(_delivery(STANDARD, _csv(STANDARD, SEPTEMBER, _cells(SEPTEMBER)), SEPTEMBER))
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED, loaded.detail
    assert len(loaded.rows) == 78 * 3
    cell = next(
        row
        for row in loaded.rows
        if (
            str(row["cohort_month"]),
            row["months_since_acquisition"],
            row["acquisition_source_set"],
        )
        == ("2025-09-01", 12, "dsp+sponsored")
    )
    assert (
        cell["as_of_month"],
        cell["time_grain"],
        cell["cohort_path"],
        cell["cohort_grain"],
        cell["acquisition_channel_group"],
        cell["cell_state"],
        cell["basis_key"],
    ) == (
        date(2026, 9, 1),
        "custom",
        "standard",
        "month",
        "dsp_and_sponsored",
        "returned",
        "ad_attributed",
    )
    assert (cell["cohort_buyers_new_to_brand_12mo"], cell["repeat_buyers"]) == (400, 40)
    assert _slices(loaded) == dict.fromkeys(SETS, (78, 0, "not_degraded"))
    assert loaded.residual == ()


def test_an_absent_cohort_writes_no_cells_and_stays_out_of_the_loss_denominator() -> None:
    """Design §6.6, review DS4-3: a cohort and group with no row at any k isn't lost cells."""
    cells = [
        cell
        for cell in _cells(SEPTEMBER)
        if (cell.cohort, cell.source_set) != ("2026-03-01", "dsp")
    ]
    loaded = _load(_delivery(STANDARD, _csv(STANDARD, SEPTEMBER, cells), SEPTEMBER))
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED, loaded.detail
    assert not [
        row
        for row in loaded.rows
        if (str(row["cohort_month"]), row["acquisition_source_set"]) == ("2026-03-01", "dsp")
    ]
    # March 2026 has six follow-up months by September; they leave the denominator.
    assert _slices(loaded)["dsp"] == (72, 0, "not_degraded")
    # Paired control: one missing k of a returned cohort is a not_returned cell, never absent.
    gap = [
        cell
        for cell in _cells(SEPTEMBER)
        if (cell.cohort, cell.k, cell.source_set) != ("2026-03-01", 2, "dsp")
    ]
    holed = _load(_delivery(STANDARD, _csv(STANDARD, SEPTEMBER, gap), SEPTEMBER))
    missing = next(
        row
        for row in holed.rows
        if (
            str(row["cohort_month"]),
            row["months_since_acquisition"],
            row["acquisition_source_set"],
        )
        == ("2026-03-01", 2, "dsp")
    )
    assert missing["cell_state"] == "not_returned"
    assert missing["repeat_buyers"] is None
    assert missing["acquisition_channel_group"] == "dsp_only"
    assert _slices(holed)["dsp"] == (78, 1, "not_degraded")


def test_a_sponsored_only_advertiser_keeps_an_intact_triangle_and_is_not_degraded() -> None:
    """Design §4.4 case 1: two absent groups never read as a 67 percent loss."""
    loaded = _load(
        _delivery(
            STANDARD,
            _csv(STANDARD, SEPTEMBER, _cells(SEPTEMBER, sets=("sponsored",))),
            SEPTEMBER,
        )
    )
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED, loaded.detail
    assert _slices(loaded) == {
        "dsp": (0, 0, "not_degraded"),
        "sponsored": (78, 0, "not_degraded"),
        "dsp+sponsored": (0, 0, "not_degraded"),
    }
    assert loaded.loss["grade"] == "not_degraded"
    assert {row["acquisition_channel_group"] for row in loaded.rows} == {"sponsored_only"}


def test_a_repeated_cohort_size_that_differs_across_k_refuses_the_delivery() -> None:
    """Design §7a: a cohort's size reads the same on every follow-up row of one delivery."""
    cells = _cells(SEPTEMBER)
    position = next(
        index
        for index, cell in enumerate(cells)
        if (cell.cohort, cell.k, cell.source_set) == ("2025-10-01", 5, "sponsored")
    )
    cells[position] = replace(cells[position], size=401)
    refused = _load(_delivery(STANDARD, _csv(STANDARD, SEPTEMBER, cells), SEPTEMBER))
    assert refused.outcome is SemanticSidecarOutcome.BLOCKED
    assert "REPEATED_TOTAL_MISMATCH" in refused.contract_rule
    assert "cohort_buyers_new_to_brand_12mo differs" in refused.contract_rule
    # Paired control: the same delivery with equal copies publishes.
    assert (
        _load(_delivery(STANDARD, _csv(STANDARD, SEPTEMBER, _cells(SEPTEMBER)), SEPTEMBER)).outcome
        is SemanticSidecarOutcome.PUBLISHED
    )


def test_two_currencies_behind_one_cohort_row_refuse_the_delivery() -> None:
    """Design §6.6, review DS4-10: MAX and MIN purchase currency differ, so amounts can't add."""
    cells = _cells(SEPTEMBER)
    cells[3] = replace(cells[3], currency="USD", currency_min="CAD")
    refused = _load(_delivery(STANDARD, _csv(STANDARD, SEPTEMBER, cells), SEPTEMBER))
    assert refused.outcome is SemanticSidecarOutcome.BLOCKED
    assert "CURRENCY_DISAGREEMENT" in refused.contract_rule
    # Pack 15's ARP currency carries the same check; control: one currency publishes.
    arp_cells = _cells(SEPTEMBER)
    arp_cells[0] = replace(arp_cells[0], currency_min="CAD")
    arp = _load(_delivery(ARP, _csv(ARP, SEPTEMBER, arp_cells), SEPTEMBER))
    assert "CURRENCY_DISAGREEMENT" in arp.contract_rule
    assert (
        _load(_delivery(ARP, _csv(ARP, SEPTEMBER, _cells(SEPTEMBER)), SEPTEMBER)).outcome
        is SemanticSidecarOutcome.PUBLISHED
    )


def test_a_cell_outside_the_vintage_triangle_refuses_the_delivery() -> None:
    """August 2026's cohort has one follow-up month by September, so k = 2 is undeclared."""
    cells = [*_cells(SEPTEMBER), _Cell("2026-08-01", 2, "dsp")]
    refused = _load(_delivery(STANDARD, _csv(STANDARD, SEPTEMBER, cells), SEPTEMBER))
    assert refused.outcome is SemanticSidecarOutcome.BLOCKED
    assert "CELL_VALUE_UNDECLARED" in refused.contract_rule
    assert "cohort triangle" in refused.contract_rule
    # Paired control: the triangle's own cells publish.
    assert (
        _load(_delivery(STANDARD, _csv(STANDARD, SEPTEMBER, _cells(SEPTEMBER)), SEPTEMBER)).outcome
        is SemanticSidecarOutcome.PUBLISHED
    )


def test_a_per_path_withheld_cell_goes_to_a_residual_keyed_by_vintage_path_and_grain() -> None:
    """Plan D19 and the FEAT-002 rule: discard the cell; never sum its currency-less revenue.

    A withheld cell of three shoppers whose repeat revenue came back without its currency is
    discarded, and its revenue is never summed.
    """
    cells = [
        cell
        for cell in _cells(SEPTEMBER)
        if (cell.cohort, cell.k, cell.source_set) != ("2026-06-01", 3, "dsp")
    ]
    loaded = _load(
        _delivery(
            STANDARD,
            _csv(STANDARD, SEPTEMBER, cells, _withheld(STANDARD, SEPTEMBER)),
            SEPTEMBER,
        )
    )
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED, loaded.detail
    (residual,) = loaded.residual
    assert (
        residual["as_of_month"],
        residual["cohort_grain"],
        residual["cohort_path"],
        residual["contract_id"],
    ) == (date(2026, 9, 1), "month", "standard", STANDARD)
    assert residual.get("repeat_revenue") is None
    # No distinct count is kept on a residual (ADR-099 point 12).
    assert residual.get("repeat_buyers") is None
    assert _slices(loaded)["dsp"] == (78, 1, "not_degraded")


def test_pack_15_publishes_arp_rows_with_retail_basis_and_its_own_integrity_count() -> None:
    loaded = _load(_delivery(ARP, _csv(ARP, SEPTEMBER, _cells(SEPTEMBER)), SEPTEMBER))
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED, loaded.detail
    cell = loaded.rows[0]
    assert (cell["cohort_path"], cell["basis_key"], cell["cohort_buyers_account_ids_check"]) == (
        "arp",
        "retail_actual",
        300,
    )
    # Pack 15 carries no cohort size of its own: the ARP rate's denominator is pack 14's.
    assert cell.get("cohort_buyers_new_to_brand_12mo") is None
    assert cell.get("cohort_buyers_account_ids") is None
    # Its own count of each cohort is kept in its load evidence, never as a figure: the
    # Retention bake compares it with pack 14's (design §6.6, review DS-13).
    totals = cast("dict[str, dict[str, str]]", loaded.loss["repeated_totals"])
    check = totals["cohort_buyers_account_ids_check"]
    assert len(check) == 12 * 3
    assert check["2025-09-01|dsp_only"] == "300"


# --- the quarterly switch (plan D9, D10) ----------------------------------------------------------


def _degraded(contract: str, period: CalendarPeriod) -> str:
    """Six of the dsp slice's 78 cells missing: 7.7 percent, past the 5 percent threshold."""
    dropped = {("2025-09-01", k, "dsp") for k in range(7, 13)}
    cells = [
        cell for cell in _cells(period) if (cell.cohort, cell.k, cell.source_set) not in dropped
    ]
    return _csv(contract, period, cells)


def _quarterly(contract: str, period: CalendarPeriod) -> str:
    return _csv(contract, period, _cells(period, triangle=QUARTERLY))


def test_a_degraded_vintage_switches_the_next_vintage_to_quarters_and_stays_on() -> None:
    store = _Store()
    first = store.load(_delivery(STANDARD, _degraded(STANDARD, JULY), JULY))
    assert first.outcome is SemanticSidecarOutcome.PUBLISHED, first.detail
    assert _slices(first)["dsp"][2] == "degraded"
    monthly = store.load(_delivery(STANDARD, _csv(STANDARD, AUGUST, _cells(AUGUST)), AUGUST))
    assert monthly.outcome is SemanticSidecarOutcome.BLOCKED
    assert "PACK_VARIANT_MISMATCH" in monthly.contract_rule
    assert "variant quarterly is due" in monthly.contract_rule
    # Positive control: the quarterly delivery of the same vintage is the one accepted.
    quarterly = store.load(
        _delivery(STANDARD, _quarterly(STANDARD, AUGUST), AUGUST, variant="quarterly")
    )
    assert quarterly.outcome is SemanticSidecarOutcome.PUBLISHED, quarterly.detail
    assert {row["cohort_grain"] for row in quarterly.rows} == {"quarter"}
    # August 2026's vintage starts in August 2025: Q4 2025 and Q1 2026 have ended follow-ups.
    assert {str(row["cohort_month"]) for row in quarterly.rows} == {"2025-10-01", "2026-01-01"}
    # Sticky until Step B: the next vintage stays quarterly too.
    september = store.load(
        _delivery(STANDARD, _csv(STANDARD, SEPTEMBER, _cells(SEPTEMBER)), SEPTEMBER)
    )
    assert september.outcome is SemanticSidecarOutcome.BLOCKED
    assert "PACK_VARIANT_MISMATCH" in september.contract_rule


def test_a_whole_monthly_vintage_keeps_months_and_refuses_a_quarterly_delivery() -> None:
    store = _Store()
    assert (
        store.load(_delivery(STANDARD, _csv(STANDARD, AUGUST, _cells(AUGUST)), AUGUST)).outcome
        is SemanticSidecarOutcome.PUBLISHED
    )
    refused = store.load(
        _delivery(STANDARD, _quarterly(STANDARD, SEPTEMBER), SEPTEMBER, variant="quarterly")
    )
    assert refused.outcome is SemanticSidecarOutcome.BLOCKED
    assert "the base pack is expected" in refused.contract_rule
    # Control: the monthly delivery of the same vintage publishes.
    monthly = store.load(
        _delivery(STANDARD, _csv(STANDARD, SEPTEMBER, _cells(SEPTEMBER)), SEPTEMBER)
    )
    assert monthly.outcome is SemanticSidecarOutcome.PUBLISHED, monthly.detail


def test_pack_15_follows_pack_14s_switch_for_the_same_marketplace() -> None:
    """Retention LLD "One grain for both paths": pack 14's evidence switches both packs."""
    store = _Store()
    assert (
        store.load(_delivery(STANDARD, _degraded(STANDARD, AUGUST), AUGUST)).outcome
        is SemanticSidecarOutcome.PUBLISHED
    )
    monthly = store.load(_delivery(ARP, _csv(ARP, SEPTEMBER, _cells(SEPTEMBER)), SEPTEMBER))
    assert monthly.outcome is SemanticSidecarOutcome.BLOCKED
    assert "PACK_VARIANT_MISMATCH" in monthly.contract_rule
    quarterly = store.load(
        _delivery(ARP, _quarterly(ARP, SEPTEMBER), SEPTEMBER, variant="quarterly")
    )
    assert quarterly.outcome is SemanticSidecarOutcome.PUBLISHED, quarterly.detail


def test_a_pack_15_only_loss_switches_neither_pack() -> None:
    """Paired control: pack 15's own degraded vintage never switches the cohort grain."""
    store = _Store()
    assert (
        store.load(_delivery(STANDARD, _csv(STANDARD, AUGUST, _cells(AUGUST)), AUGUST)).outcome
        is SemanticSidecarOutcome.PUBLISHED
    )
    degraded_arp = store.load(_delivery(ARP, _degraded(ARP, AUGUST), AUGUST))
    assert degraded_arp.outcome is SemanticSidecarOutcome.PUBLISHED, degraded_arp.detail
    assert _slices(degraded_arp)["dsp"][2] == "degraded"
    for contract in (STANDARD, ARP):
        loaded = store.load(
            _delivery(contract, _csv(contract, SEPTEMBER, _cells(SEPTEMBER)), SEPTEMBER)
        )
        assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED, (contract, loaded.detail)


def test_pack_15_without_pack_14_governed_fails_closed() -> None:
    """A followed contract that isn't governed leaves the switch state unreadable.

    UNESTABLISHED: nothing loads rather than the base pack being guessed.
    """
    store = _Store()
    delivery = _delivery(ARP, _csv(ARP, SEPTEMBER, _cells(SEPTEMBER)), SEPTEMBER)
    contracts = tuple(contract for _, contract in load_contracts(ROOT))
    arp = next(contract for contract in contracts if contract.contract_id == ARP)
    request = VariantEvidenceRequest(
        manifest=delivery.manifest,
        contracts=(arp,),
        discard_policies=discard_policies(load_query_packs(ROOT)),
        packs=store.tooling.packs,
        bridge=load_commerce_identity_bridge(ROOT),
        publications=store.tooling.publications,
    )
    assert not read_variant_evidence(request)[ARP].established
    # Control: with pack 14 governed and nothing published before, nothing is due.
    assert read_variant_evidence(replace(request, governed=contracts)) == {}


def test_the_monthly_and_quarterly_vintage_texts_share_one_cohort_set_with_pack_15() -> None:
    """Design §6.6: pack 15's cohort set is byte-identical to pack 14's (review DS-13)."""
    marker = re.compile(r"/\* cohort-set start \*/.*?/\* cohort-set end \*/", re.DOTALL)
    probe = (ROOT / "validation/sql/bo/pack14.ctes.sql").read_text()
    (expected,) = marker.findall(probe)
    packs = ROOT / "contracts/amc/query-packs"
    texts = {
        name: (packs / f"{name}.sql").read_text()
        for contract in (STANDARD, ARP)
        for name in (contract, f"{contract}-quarterly")
    }
    for name, sql in texts.items():
        assert marker.findall(sql) == [expected], name
    # Each file is the governed text its pack and variant bind.
    for contract in (STANDARD, ARP):
        pack = pack_by_id(ROOT, contract)
        assert pack.sql == " ".join(texts[contract].split()) or pack.sql == texts[contract]
    # The ARP text restricts to account IDs after the set, reads its own marketplace parameter
    # and counts orders by purchase_id; pack 14 keeps the probe-compiled forms (plan D16).
    arp = texts[ARP]
    assert arp.index("WHERE user_id_type = 'adUserId'") > arp.index("/* cohort-set end */")
    assert "CUSTOM_PARAMETER('arp_marketplace_name')" in arp
    assert "COUNT(DISTINCT p.purchase_id)" in arp
    standard = texts[STANDARD]
    assert "CAST(CUSTOM_PARAMETER('as_of_month') AS TIMESTAMP)" in standard
    assert "/ 2629800.0)" in standard
    assert "months AS (SELECT DISTINCT k FROM repeats WHERE k BETWEEN 1 AND 12)" in standard
    # Control: a text with one changed character in its set doesn't match.
    assert marker.findall(standard.replace("MIN(d) AS acq_date", "MIN(d) AS acq_day")) != [expected]


# --- Chat: one vintage, one path, one cohort grain (ADR-104, D-8) --------------------------------

CATALOG = load_catalog(ROOT)
COHORT_CONTRACTS = tuple(
    contract for _, contract in load_contracts(ROOT) if contract.target_table == FACT
)
_CHAT_GRAINS = ("cohort", "months_since_acquisition", "acquisition_channel_group")


@dataclass
class _Rows:
    seen: list[QueryRequest]

    def run(self, request: QueryRequest) -> QueryResult:
        self.seen.append(request)
        logical = request.plan.logical
        keys = {
            "cohort_month": "2025-10-01",
            "months_since_acquisition": 1,
            "acquisition_channel_group": "sponsored_only",
        }
        row: QueryRow = {column: keys[column] for column in logical.group_by}
        row.update(dict.fromkeys(logical.carried_columns, "USD"))
        for index in range(len(logical.measures)):
            row[measure_alias(index)] = 40 if index == 0 else 400
        return QueryResult(
            rows=(row,), bytes_scanned=1, truncated=False, suppression=Suppression(False)
        )


@dataclass
class _Vintages:
    series: Mapping[CalendarPeriod, tuple[str | None, ...]]

    def served_periods(self, request: ServedPeriodsRequest) -> ServedPeriods:
        published = tuple(contract.contract_id for contract in request.contracts)
        return ServedPeriods(
            frozenset((contract, period) for contract in published for period in self.series),
            dict.fromkeys(published, 2),
            {
                (contract, period): held
                for contract in published
                for period, held in self.series.items()
            },
        )


def _ask(  # noqa: PLR0913 -- a question: metric, filters, served vintages and a prior
    metric: str,
    filters: dict[str, str],
    series: Mapping[CalendarPeriod, tuple[str | None, ...]],
    *,
    prior: CalendarPeriod | None = None,
    grains: tuple[str, ...] = _CHAT_GRAINS,
) -> _Rows:
    queries = _Rows([])
    handle(
        MeasurementToolRequest(
            tool=MeasurementTool.METRIC_BY_GRAIN,
            catalog=CATALOG,
            contracts=COHORT_CONTRACTS,
            metric_name=metric,
            grains=grains,
            window=SEPTEMBER.window,
            prior_window=None if prior is None else prior.window,
            computed_at=NOW,
            correlation_id="corr-cohorts",
            filters={"marketplace_key": "amazon_us", **filters},
        ),
        MeasurementTooling(
            queries=queries, publications=StubPublications(), periods=_Vintages(series)
        ),
    )
    return queries


def test_chat_reads_one_vintage_one_path_and_its_cohort_grain() -> None:
    queries = _ask("repeat_buyer_rate", {"cohort_path": "standard"}, {SEPTEMBER: (None,)})
    (request,) = queries.seen
    assert dict(request.plan.logical.filters) == {
        "time_grain": "custom",
        "as_of_month": "2026-09-01",
        "grain_set": "platform",
        "marketplace_key": "amazon_us",
        "cohort_path": "standard",
        "cohort_grain": "month",
    }
    assert request.plan.logical.measures == ("repeat_buyers", "cohort_buyers_new_to_brand_12mo")
    # The quarterly series, once switched on, is read on its own cohort grain.
    quarterly = _ask("cohort_repeat_buyers", {"cohort_path": "arp"}, {SEPTEMBER: ("quarterly",)})
    assert quarterly.seen[0].plan.logical.filters["cohort_grain"] == "quarter"


def test_as_of_month_is_an_exact_selector_and_the_latest_vintage_is_the_default() -> None:
    both = {AUGUST: (None,), SEPTEMBER: (None,)}
    named = _ask(
        "cohort_repeat_buyers", {"cohort_path": "standard", "as_of_month": "2026-08"}, both
    )
    assert named.seen[0].plan.logical.filters["as_of_month"] == "2026-08-01"
    with pytest.raises(ValidationError, match="isn't a month"):
        _ask("cohort_repeat_buyers", {"cohort_path": "standard", "as_of_month": "August"}, both)
    # A trailing suffix is refused, never read as the month before it; the first-day form plans.
    for malformed in ("2026-08garbage", "2026-08-15", "2026-08-01x"):
        with pytest.raises(ValidationError, match="isn't a month"):
            _ask(
                "cohort_repeat_buyers", {"cohort_path": "standard", "as_of_month": malformed}, both
            )
    first_day = _ask(
        "cohort_repeat_buyers", {"cohort_path": "standard", "as_of_month": "2026-08-01"}, both
    )
    assert first_day.seen[0].plan.logical.filters["as_of_month"] == "2026-08-01"


def test_a_cohort_figure_without_its_path_is_refused_and_a_standard_rate_refuses_arp() -> None:
    with pytest.raises(DomainError) as caught:
        _ask("cohort_repeat_buyers", {}, {SEPTEMBER: (None,)})
    assert caught.value.context["reason"] == "requires_source_option"
    assert "Pin cohort_path." in caught.value.description
    with pytest.raises(DomainError) as caught:
        _ask("repeat_buyer_rate", {"cohort_path": "arp"}, {SEPTEMBER: (None,)})
    assert caught.value.context["reason"] == "requires_source_option"
    # Paired control: the same figure on the path it reads plans.
    assert _ask("cohort_repeat_buyers", {"cohort_path": "arp"}, {SEPTEMBER: (None,)}).seen


@pytest.mark.parametrize(
    ("metric", "path"),
    [
        ("cohort_buyers_account_ids", "standard"),
        ("cohort_repeat_buyers", "standard"),
        ("cohort_repeat_purchases", "arp"),
    ],
)
def test_a_sum_across_cohorts_of_one_whole_vintage_is_refused(metric: str, path: str) -> None:
    # D-3, D-9: cohort counts are never summed across cohorts, even inside one fully returned
    # vintage, so the cohort axis is exact only and a breakdown that leaves it out is refused.
    filters = {"cohort_path": path}
    collapsed = ("months_since_acquisition", "acquisition_channel_group")
    with pytest.raises(DomainError, match="cohort"):
        _ask(metric, filters, {SEPTEMBER: (None,)}, grains=collapsed)
    # Paired control: the same figure grouped by cohort plans, one row per cohort.
    (request,) = _ask(metric, filters, {SEPTEMBER: (None,)}).seen
    assert "cohort_month" in request.plan.logical.group_by


def _bound(path: str, absent: frozenset[str] = frozenset()) -> frozenset[str]:
    """Bind one path's cohort read and return the publication partitions it needs."""
    (request,) = _ask("cohort_repeat_buyers", {"cohort_path": path}, {SEPTEMBER: (None,)}).seen
    selector = PublicationSelector(
        _Current(absent=absent), load_source_packs(ROOT), load_commerce_identity_bridge(ROOT)
    )
    bound = selector.select(
        PublicationSelectionRequest((request.plan.logical,), COHORT_CONTRACTS, NOW)
    )
    return frozenset(item.partition.identity for item in bound.plans[0].primary)


def test_each_cohort_path_binds_its_own_pack_and_never_the_other() -> None:
    # Both packs write fct_shopper_acquisition; each stamps its own cohort_path literal, so a read
    # pinning one path needs only that pack's publication (ADR-104).
    standard, arp = _bound("standard"), _bound("arp")
    assert len(standard) == len(arp) == 1
    assert not standard & arp
    # The optional paid ARP pack never blocks the standard path: with pack 15 unpublished, the
    # standard read still binds pack 14's vintage.
    assert _bound("standard", absent=arp) == standard
    # Paired refusals: with its own pack unpublished, a path is refused, never served from the
    # other path's pack.
    for path, own in (("standard", standard), ("arp", arp)):
        with pytest.raises(DomainError) as refused:
            _bound(path, absent=own)
        assert refused.value.code == SELECTION_FAILED


def test_chat_pairs_the_arp_rate_operands_and_one_plan_never_divides_them() -> None:
    # Owner decision 2026-10-12: chat answers the ARP rates (test_retention_source checks the
    # rate, the cohort-size check and the absent paid path). Each operand is planned on its own
    # path for one vintage.
    queries = _ask("repeat_buyer_rate_arp", {}, {SEPTEMBER: (None,)})
    assert sorted(
        (r.plan.logical.filters["cohort_path"], r.plan.logical.metric.name) for r in queries.seen
    ) == [("arp", "cohort_repeat_buyers"), ("standard", "cohort_buyers_account_ids")]
    assert {r.plan.logical.filters["as_of_month"] for r in queries.seen} == {"2026-09-01"}
    # A single plan can't read two deliveries, so the planner refuses one, offering its operands.
    with pytest.raises(DomainError) as caught:
        plan(
            CATALOG,
            PlanRequest(
                "repeat_buyer_rate_arp", _CHAT_GRAINS, SEPTEMBER.window, {"time_grain": "custom"}
            ),
            FactShape.for_fact(FACT, COHORT_CONTRACTS),
        )
    assert caught.value.context["reason"] == "not_yet_computable"
    assert caught.value.description.endswith(
        "Available instead: cohort_repeat_buyers, cohort_buyers_account_ids."
    )


def test_chat_refuses_a_comparison_across_cohort_grains() -> None:
    # Two vintages compare only when their 13-month windows don't share a day.
    earlier = vintage_ending(date(2025, 8, 1))
    switched = {earlier: (None,), SEPTEMBER: ("quarterly",)}
    with pytest.raises(ValidationError) as caught:
        _ask("cohort_repeat_buyers", {"cohort_path": "standard"}, switched, prior=earlier)
    assert caught.value.description.endswith("Cohort grain changed in September 2026.")
    assert caught.value.context["reason"] == "cohort_grain_changed"
    # Paired control: two monthly vintages compare, each on its own vintage's rows.
    monthly = _ask(
        "cohort_repeat_buyers",
        {"cohort_path": "standard"},
        {earlier: (None,), SEPTEMBER: (None,)},
        prior=earlier,
    )
    assert len(monthly.seen) == 2
