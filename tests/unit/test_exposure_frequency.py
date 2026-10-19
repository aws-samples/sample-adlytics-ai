# SPDX-License-Identifier: MIT-0
"""Packs 6 and 7, `amc-exposure-frequency-*`: declared buckets, the coarse switch and the planner.

Every delivery here is fictional. The load runs the real ingestion path (assessment, then the
sidecar) on in-memory tooling, as the pack 5 tests do; the variant switch is exercised over
consecutive months on one publication store that reads each series' own scope (plan D10).
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from publication_support import StubPublications
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
from adapters.jobs.fixture_generation.catalog import load_synthetic_entity_catalog
from adapters.jobs.ingest_delivery.landing import LoadedDelivery
from adapters.jobs.ingest_delivery.semantic_ingestion import (
    SemanticIngestionRequest,
    ingest_semantic_delivery,
)
from adapters.jobs.ingest_delivery.semantic_sidecar import (
    SemanticSidecarOutcome,
    SemanticSidecarTooling,
)
from adapters.jobs.land_fixtures.bundle import (
    LandingError,
    _periodic_runs,  # pyright: ignore[reportPrivateUsage]
    _SourceRunsRequest,  # pyright: ignore[reportPrivateUsage]
    _variant_options,  # pyright: ignore[reportPrivateUsage]
)
from adapters.outbound.metric_dictionary import load_catalog
from adapters.producer.packs import (
    discard_policies,
    installed_governance,
    load_query_packs,
    pack_by_id,
)
from domain.catalog.capability import RefusalReason
from domain.curation.ingestion import IngestionError
from domain.curation.landing import GOVERNED_MANIFEST_LAYOUT, LandedObject
from domain.measurement.figures import measure
from domain.measurement.planning import FactShape, PlanRequest, measure_alias, plan
from domain.measurement.rosters import Roster, RosterEntry, RosterKind
from domain.narrative.shaped_response import UnitKind, Units
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.publication import PublicationScope, SuccessfulPublication
from domain.semantics.source_aligned import LandedDeliveryRef
from domain.semantics.time import TimeGrain
from domain.shared_kernel.errors import DomainError, InvariantError, ValidationError
from domain.shared_kernel.provenance import Suppression
from domain.shared_kernel.values import DateRange
from ports.outbound.canonical import CanonicalDestination
from ports.outbound.query import QueryRequest, QueryResult, QueryRow
from ports.outbound.rosters import RosterRequest
from ports.outbound.served_periods import ServedPeriods, ServedPeriodsRequest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-099", "ADR-101", "ADR-102")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/generated"
PLATFORM = "amc-exposure-frequency-platform"
CHANNEL = "amc-exposure-frequency-channel"
FACT = "fct_exposure_frequency"
NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)
FINE = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10_plus")
COARSE = ("1", "2", "3", "4_6", "7_9", "10_plus")
SPACES = ("account", "device_or_browser")
JUNE = CalendarPeriod(date(2026, 6, 1), date(2026, 6, 30), TimeGrain.MONTH)
JULY = CalendarPeriod(date(2026, 7, 1), date(2026, 7, 31), TimeGrain.MONTH)
AUGUST = CalendarPeriod(date(2026, 8, 1), date(2026, 8, 31), TimeGrain.MONTH)
SEPTEMBER = CalendarPeriod(date(2026, 9, 1), date(2026, 9, 30), TimeGrain.MONTH)
Q2 = CalendarPeriod(date(2026, 4, 1), date(2026, 6, 30), TimeGrain.QUARTER)
US = "amazon_us"
_TRAILER = (
    "marketplace_name,time_window_start,time_window_end,distinct_user_count,"
    "suppression_reason,rows_suppressed"
)
_MEASURES = (
    "bucket_scheme,row_kind,shoppers,impressions,dsp_window_spend,sponsored_window_spend,"
    "dsp_currency,dsp_currency_min,sponsored_currency,sponsored_currency_min,purchasers,"
    "ntb_purchasers,purchases,revenue_incl_halo,currency_code,purchase_currency_min,"
)
HEADERS = {
    PLATFORM: f"frequency_bucket,identity_space,{_MEASURES}{_TRAILER}",
    CHANNEL: f"ad_product_type,frequency_bucket,identity_space,{_MEASURES}{_TRAILER}",
}


def _window(period: CalendarPeriod) -> str:
    end = period.end + timedelta(days=1)
    return f"{period.start.isoformat()}T00:00:00.000Z,{end.isoformat()}T00:00:00.000Z"


def _imps(bucket: str) -> int:
    return {"10_plus": 12, "4_6": 5, "7_9": 8}.get(bucket) or int(bucket)


@dataclass(frozen=True)
class _Cell:
    bucket: str
    space: str
    scheme: str = "fine"
    shoppers: int = 1000
    purchasers: int = 50
    ad_product: str | None = None


def _row(cell: _Cell, period: CalendarPeriod) -> str:
    shoppers = cell.shoppers
    impressions = shoppers * _imps(cell.bucket)
    head = "" if cell.ad_product is None else f"{cell.ad_product},"
    return (
        f"{head}{cell.bucket},{cell.space},{cell.scheme},window,{shoppers},{impressions},"
        f"600000,120000000,USD,USD,USD,USD,{cell.purchasers},20,60,1950.00,USD,USD,"
        f"AMAZON.COM,{_window(period)},2,,false"
    )


def _withheld(bucket: str, period: CalendarPeriod, *, ad_product: bool = False) -> str:
    """Return a per-path withheld row of three shoppers (plan D19), purchase sums NULL."""
    head = "," if ad_product else ""
    return (
        f"{head}{bucket},,fine,window,3,36,0,137156329,,,,,2,1,,,,,,{_window(period)},1,"
        "1 'sponsored_ads_traffic' (path 2 of 3) users < 2 required,true"
    )


def _grid(  # noqa: PLR0913 -- one fictional grid, by keyword
    period: CalendarPeriod,
    *,
    scheme: str = "fine",
    ad_product: str | None = None,
    skip: tuple[tuple[str, str], ...] = (),
    overrides: Mapping[tuple[str, str], Mapping[str, object]] | None = None,
) -> list[str]:
    buckets = FINE if scheme == "fine" else COARSE
    return [
        _row(
            replace(
                _Cell(bucket, space, scheme, ad_product=ad_product),
                **(overrides or {}).get((bucket, space), {}),  # type: ignore[arg-type]
            ),
            period,
        )
        for space in SPACES
        for bucket in buckets
        if (bucket, space) not in skip
    ]


def _csv(contract: str, *lines: str) -> str:
    return "".join(f"{line}\n" for line in (HEADERS[contract], *lines))


def _delivery(  # noqa: PLR0913 -- the contract, its text, its period and a variant
    contract: str, text: str, period: CalendarPeriod, *, variant: str | None = None
) -> LoadedDelivery:
    body = text.encode()
    declaration = json.loads((FIXTURES / "amc-exposure-groups.manifest.json").read_text())
    pack = pack_by_id(ROOT, contract)
    binding = pack if variant is None else pack.variant_binding(variant)
    options = {
        "period_grain": period.grain.value,
        **({} if variant is None else {"pack_variant": variant}),
    }
    manifest = build_manifest(
        BuildManifestRequest(
            declaration={
                **declaration,
                "option_set": options,
                "period_start": period.start.isoformat(),
                "period_end": period.end.isoformat(),
                "source_watermark": (period.end + timedelta(days=16)).isoformat(),
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
                contracts=tuple(
                    contract
                    for _, contract in load_contracts(ROOT)
                    if contract.contract_id == contract_id
                ),
                columns=load_registry(ROOT),
                now=NOW,
                operation_id="exposure-frequency-unit",
                execution_id="exposure-frequency-execution",
                plan_digest="sha256:exposure-frequency-plan",
                installed_governance=installed_governance(packs),
                discard_policies=discard_policies(packs),
            ),
            self.tooling,
        )
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
                    result.detail
                    for item in candidates
                    for result in item.publication.evidence.results
                    if result.rule_id == rule
                ),
                "",
            )

        loss = detail(f"suppression:{contract_id}.cell-loss.v1")
        return _Loaded(
            results[0].outcome,
            results[0].detail,
            rows(FACT),
            rows(f"{FACT}_withheld"),
            json.loads(loss) if loss else {},
            detail(f"contract.{contract_id}.v1"),
        )


def _load(delivery: LoadedDelivery) -> _Loaded:
    return _Store().load(delivery)


def _slices(loaded: _Loaded) -> dict[tuple[str, ...], tuple[int, str]]:
    return {
        tuple(str(value) for value in cast("dict[str, object]", item["slice"]).values()): (
            cast("int", item["not_returned_cells"]),
            cast("str", item["grade"]),
        )
        for item in cast("list[dict[str, object]]", loaded.loss["slices"])
    }


# --- the load -----------------------------------------------------------------------------------


def test_a_whole_fine_month_publishes_every_bucket_at_the_platform_grain_set() -> None:
    loaded = _load(_delivery(PLATFORM, _csv(PLATFORM, *_grid(AUGUST)), AUGUST))
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED, loaded.detail
    assert len(loaded.rows) == 20
    cell = next(
        row
        for row in loaded.rows
        if (row["frequency_bucket"], row["identity_space"]) == ("10_plus", "account")
    )
    assert (cell["grain_set"], cell["bucket_scheme"], cell["row_kind"], cell["cell_state"]) == (
        "platform",
        "fine",
        "window",
        "returned",
    )
    assert (cell["shoppers"], cell["impressions"], cell["purchasers"]) == (1000, 12000, 50)
    # Each spend scaled by its own column's divisor and labelled by its own currency.
    assert (str(cell["dsp_window_spend"]), str(cell["sponsored_window_spend"])) == (
        "6.0000",
        "1.2000",
    )
    # The credit branch isn't built (plan D12): no model, and no media at the platform grain set.
    assert all(cell.get(column) is None for column in ("attribution_model_key", "ad_product_key"))
    assert _slices(loaded) == {
        ("account",): (0, "not_degraded"),
        ("device_or_browser",): (0, "not_degraded"),
    }
    assert loaded.residual == ()


def test_pack_7_fills_every_bucket_per_returned_ad_product_with_its_resolved_media() -> None:
    """Design §4.5: the enumeration applies to each ad product that returned at least one row."""
    products = _grid(AUGUST, ad_product="sponsored_products", skip=(("9", "account"),))
    dsp = [
        line for line in _grid(AUGUST, ad_product="amazon_dsp") if ",device_or_browser," not in line
    ]
    loaded = _load(_delivery(CHANNEL, _csv(CHANNEL, *products, *dsp), AUGUST))
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED, loaded.detail
    assert len(loaded.rows) == 40
    gap = next(
        row
        for row in loaded.rows
        if (row["ad_product_key"], row["frequency_bucket"], row["identity_space"])
        == ("sponsored_products", "9", "account")
    )
    returned = next(
        row
        for row in loaded.rows
        if row["ad_product_key"] == "sponsored_products" and row["cell_state"] == "returned"
    )
    assert gap["cell_state"] == "not_returned"
    assert gap["shoppers"] is None
    # A gap's channel follows from its ad product, as its returned siblings' do.
    for column in ("channel_key", "channel_group_key", "ecosystem_key", "platform_key"):
        assert gap[column] == returned[column] is not None, column
    assert (gap["grain_set"], gap["bucket_scheme"], gap["row_kind"]) == (
        "channel",
        "fine",
        "window",
    )
    # Graded per identity space and ad product: DSP returned no device-or-browser row at all.
    assert _slices(loaded) == {
        ("amazon_dsp", "account"): (0, "not_degraded"),
        ("sponsored_products", "account"): (1, "degraded"),
        ("amazon_dsp", "device_or_browser"): (10, "degraded"),
        ("sponsored_products", "device_or_browser"): (0, "not_degraded"),
    }


def test_a_null_bucket_on_an_unsuppressed_row_refuses_the_delivery() -> None:
    """Design §4.5, review DS3-2: the CASE has no ELSE, so a NULL bucket means the fence leaked."""
    lines = _grid(AUGUST)
    lines[0] = lines[0].replace("1,account,", ",account,", 1)
    refused = _load(_delivery(PLATFORM, _csv(PLATFORM, *lines), AUGUST))
    assert refused.outcome is SemanticSidecarOutcome.BLOCKED
    assert "FREQUENCY_BUCKET_NULL" in refused.contract_rule
    # Paired control: the same delivery with its bucket publishes.
    assert (
        _load(_delivery(PLATFORM, _csv(PLATFORM, *_grid(AUGUST)), AUGUST)).outcome
        is SemanticSidecarOutcome.PUBLISHED
    )


def test_a_per_path_withheld_tail_bucket_is_discarded_and_degrades_its_slice() -> None:
    """Plan D19: a withheld tail bucket of three shoppers is discarded like any withheld row."""
    lines = _grid(AUGUST, skip=(("10_plus", "device_or_browser"),))
    loaded = _load(
        _delivery(PLATFORM, _csv(PLATFORM, *lines, _withheld("10_plus", AUGUST)), AUGUST)
    )
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED, loaded.detail
    gap = next(
        row
        for row in loaded.rows
        if (row["frequency_bucket"], row["identity_space"]) == ("10_plus", "device_or_browser")
    )
    assert gap["cell_state"] == "not_returned"
    assert all(gap[column] is None for column in ("shoppers", "impressions", "purchasers"))
    assert _slices(loaded) == {
        ("account",): (0, "not_degraded"),
        ("device_or_browser",): (1, "degraded"),
    }
    # Its impressions go to the residual; its sponsored spend came back without its currency and
    # is never summed (FEAT-002 rule); no distinct count is kept.
    (residual,) = loaded.residual
    assert (residual["row_kind"], residual["impressions"]) == ("window", 36)
    assert residual.get("sponsored_window_spend") is None
    assert residual.get("shoppers") is None
    # Paired control: the same row keeping its sponsored currency is summed.
    kept = _withheld("10_plus", AUGUST).replace("137156329,,,,,", "137156329,,,USD,USD,", 1)
    control = _load(_delivery(PLATFORM, _csv(PLATFORM, *lines, kept), AUGUST))
    (summed,) = control.residual
    assert summed["sponsored_window_spend"] is not None


# --- the automatic coarse switch (plan D9, D10) ---------------------------------------------------


def _degraded(period: CalendarPeriod) -> list[str]:
    return [
        *_grid(period, skip=(("10_plus", "device_or_browser"),)),
        _withheld("10_plus", period),
    ]


def test_a_degraded_fine_month_switches_the_next_month_to_coarse_and_refuses_fine() -> None:
    store = _Store()
    first = store.load(_delivery(PLATFORM, _csv(PLATFORM, *_degraded(JUNE)), JUNE))
    assert first.outcome is SemanticSidecarOutcome.PUBLISHED, first.detail
    fine = store.load(_delivery(PLATFORM, _csv(PLATFORM, *_grid(JULY)), JULY))
    assert fine.outcome is SemanticSidecarOutcome.BLOCKED
    assert "PACK_VARIANT_MISMATCH" in fine.contract_rule
    assert "variant coarse is due" in fine.contract_rule
    # Positive control: the coarse delivery of the same month is the one accepted.
    coarse = store.load(
        _delivery(
            PLATFORM,
            _csv(PLATFORM, *_grid(JULY, scheme="coarse")),
            JULY,
            variant="coarse",
        )
    )
    assert coarse.outcome is SemanticSidecarOutcome.PUBLISHED, coarse.detail
    assert {row["bucket_scheme"] for row in coarse.rows} == {"coarse"}
    assert sorted({str(row["frequency_bucket"]) for row in coarse.rows}) == sorted(COARSE)
    # Sticky until Step B: a whole coarse month keeps the next month coarse too.
    august_fine = store.load(_delivery(PLATFORM, _csv(PLATFORM, *_grid(AUGUST)), AUGUST))
    assert august_fine.outcome is SemanticSidecarOutcome.BLOCKED
    assert "PACK_VARIANT_MISMATCH" in august_fine.contract_rule


def test_a_whole_fine_month_keeps_fine_and_refuses_a_coarse_delivery() -> None:
    store = _Store()
    assert (
        store.load(_delivery(PLATFORM, _csv(PLATFORM, *_grid(JUNE)), JUNE)).outcome
        is SemanticSidecarOutcome.PUBLISHED
    )
    coarse = store.load(
        _delivery(
            PLATFORM,
            _csv(PLATFORM, *_grid(JULY, scheme="coarse")),
            JULY,
            variant="coarse",
        )
    )
    assert coarse.outcome is SemanticSidecarOutcome.BLOCKED
    assert "the base pack is expected" in coarse.contract_rule
    # Control: the fine delivery of the same month publishes.
    fine = store.load(_delivery(PLATFORM, _csv(PLATFORM, *_grid(JULY)), JULY))
    assert fine.outcome is SemanticSidecarOutcome.PUBLISHED, fine.detail


def test_the_month_and_quarter_series_switch_independently() -> None:
    """The period grain is in the option set: a degraded month doesn't switch the quarter."""
    store = _Store()
    assert (
        store.load(_delivery(PLATFORM, _csv(PLATFORM, *_degraded(JUNE)), JUNE)).outcome
        is SemanticSidecarOutcome.PUBLISHED
    )
    quarter = store.load(_delivery(PLATFORM, _csv(PLATFORM, *_grid(Q2)), Q2))
    assert quarter.outcome is SemanticSidecarOutcome.PUBLISHED, quarter.detail
    # Control: the month series after it is coarse.
    month = store.load(_delivery(PLATFORM, _csv(PLATFORM, *_grid(JULY)), JULY))
    assert month.outcome is SemanticSidecarOutcome.BLOCKED


def test_any_degraded_ad_product_slice_switches_pack_7_to_coarse() -> None:
    """Design §5.5: for pack 7 a slice is an identity space and an ad product."""
    store = _Store()
    lines = [
        *_grid(JUNE, ad_product="sponsored_products"),
        *_grid(JUNE, ad_product="sponsored_brands", skip=(("8", "account"),)),
    ]
    assert (
        store.load(_delivery(CHANNEL, _csv(CHANNEL, *lines), JUNE)).outcome
        is SemanticSidecarOutcome.PUBLISHED
    )
    fine = [
        *_grid(JULY, ad_product="sponsored_products"),
        *_grid(JULY, ad_product="sponsored_brands"),
    ]
    refused = store.load(_delivery(CHANNEL, _csv(CHANNEL, *fine), JULY))
    assert refused.outcome is SemanticSidecarOutcome.BLOCKED
    assert "PACK_VARIANT_MISMATCH" in refused.contract_rule
    coarse = [
        *_grid(JULY, scheme="coarse", ad_product="sponsored_products"),
        *_grid(JULY, scheme="coarse", ad_product="sponsored_brands"),
    ]
    accepted = store.load(_delivery(CHANNEL, _csv(CHANNEL, *coarse), JULY, variant="coarse"))
    assert accepted.outcome is SemanticSidecarOutcome.PUBLISHED, accepted.detail


def test_a_coarse_delivery_returning_fine_buckets_is_refused() -> None:
    """The coarse variant's own declared cells: a fine bucket isn't one of them."""
    store = _Store()
    store.load(_delivery(PLATFORM, _csv(PLATFORM, *_degraded(JUNE)), JUNE))
    wrong = [line.replace(",fine,", ",coarse,", 1) for line in _grid(JULY)]
    refused = store.load(_delivery(PLATFORM, _csv(PLATFORM, *wrong), JULY, variant="coarse"))
    assert refused.outcome is SemanticSidecarOutcome.BLOCKED
    assert "CELL_VALUE_UNDECLARED" in refused.contract_rule


# --- the residual names its bucket scheme (plan D11) ---------------------------------------------


def _v2_tables(section: str) -> dict[str, dict[str, object]]:
    document = json.loads((ROOT / "contracts/generated/v2-tables.json").read_text())
    return {str(table["name"]): table for table in document[section]}


def _columns(table: Mapping[str, object]) -> dict[str, bool]:
    return {
        str(column["name"]): bool(column["nullable"])
        for column in cast("list[dict[str, object]]", table["columns"])
    }


@pytest.mark.parametrize("section", ["canonicalCandidateTables", "canonicalCurrentTables"])
def test_the_fact_and_its_residual_carry_bucket_scheme_row_kind_and_model_from_creation(
    section: str,
) -> None:
    tables = _v2_tables(section)
    for name in (FACT, f"{FACT}_withheld"):
        columns = _columns(tables[name])
        grain = cast("list[str]", tables[name]["grain"])
        assert (columns["bucket_scheme"], columns["row_kind"]) == (False, False), name
        assert {"bucket_scheme", "row_kind"} <= set(grain), name
        # The credit branch isn't built (plan D12): the model column is there, nullable.
        assert columns["attribution_model_key"] is True, name
        assert "attribution_model_key" not in grain, name
    # Control: pack 5's residual keys on its row kind and has no bucket scheme.
    outcome = _columns(tables["fct_exposure_group_outcome_withheld"])
    assert outcome["row_kind"] is False
    assert "bucket_scheme" not in outcome


def test_a_withheld_bucket_lands_on_a_residual_keyed_by_its_scheme() -> None:
    lines = _grid(AUGUST, skip=(("10_plus", "device_or_browser"),))
    fine = _load(_delivery(PLATFORM, _csv(PLATFORM, *lines, _withheld("10_plus", AUGUST)), AUGUST))
    assert fine.outcome is SemanticSidecarOutcome.PUBLISHED, fine.detail
    assert [(row["bucket_scheme"], row["row_kind"]) for row in fine.residual] == [
        ("fine", "window")
    ]
    # Control: a coarse delivery's withheld bucket lands under the coarse scheme.
    store = _Store()
    store.load(_delivery(PLATFORM, _csv(PLATFORM, *_degraded(JUNE)), JUNE))
    coarse_lines = [
        *_grid(JULY, scheme="coarse", skip=(("10_plus", "device_or_browser"),)),
        _withheld("10_plus", JULY).replace(",fine,", ",coarse,", 1),
    ]
    coarse = store.load(_delivery(PLATFORM, _csv(PLATFORM, *coarse_lines), JULY, variant="coarse"))
    assert coarse.outcome is SemanticSidecarOutcome.PUBLISHED, coarse.detail
    assert [row["bucket_scheme"] for row in coarse.residual] == ["coarse"]


def test_a_withheld_row_without_its_bucket_scheme_refuses_the_delivery() -> None:
    lines = _grid(AUGUST, skip=(("10_plus", "device_or_browser"),))
    blank = _withheld("10_plus", AUGUST).replace(",fine,", ",,", 1)
    # Fails closed as a missing row_kind does: the delivery is refused before anything is written.
    with pytest.raises(IngestionError, match="bucket_scheme is absent"):
        _load(_delivery(PLATFORM, _csv(PLATFORM, *lines, blank), AUGUST))
    # Two schemes among one delivery's withheld rows can't share one residual key either.
    other = _withheld("9", AUGUST).replace(",fine,", ",coarse,", 1)
    mixed = [line for line in lines if not line.startswith("9,device_or_browser,")]
    with pytest.raises(IngestionError, match="one delivery is counted under one"):
        _load(
            _delivery(PLATFORM, _csv(PLATFORM, *mixed, _withheld("10_plus", AUGUST), other), AUGUST)
        )
    # Paired control: the same withheld row carrying its scheme publishes.
    kept = _load(_delivery(PLATFORM, _csv(PLATFORM, *lines, _withheld("10_plus", AUGUST)), AUGUST))
    assert kept.outcome is SemanticSidecarOutcome.PUBLISHED, kept.detail


# --- the dictionary and the planner ---------------------------------------------------------------

CATALOG = load_catalog(ROOT)
CONTRACTS = tuple(contract for _, contract in load_contracts(ROOT))
FREQUENCY_METRICS = (
    "shoppers_in_frequency_bucket",
    "purchasers_in_frequency_bucket",
    "ntb_purchasers_in_frequency_bucket",
    "impressions_in_frequency_bucket",
    "dsp_window_spend_in_frequency_bucket",
    "sponsored_window_spend_in_frequency_bucket",
    "window_spend_in_frequency_bucket",
    "purchases_in_frequency_bucket",
    "revenue_incl_halo_in_frequency_bucket",
    "purchaser_rate_by_frequency",
    "roas_incl_halo_by_frequency",
)


def test_every_frequency_metric_is_causal_false_and_no_credited_metric_is_declared() -> None:
    for name in FREQUENCY_METRICS:
        metric = CATALOG.metric(name)
        assert metric is not None, name
        assert metric.causal is False, name
    for name in ("purchaser_rate_by_frequency", "roas_incl_halo_by_frequency"):
        assert CATALOG.metric(name).requires == (("identity_space", "account"),)  # type: ignore[union-attr]
    # Plan D12: pack 7 ships without its credit branch, so no credited metric exists.
    for name in (
        "credited_purchases_in_frequency_bucket",
        "credited_revenue_incl_halo_in_frequency_bucket",
        "roas_credited_incl_halo_by_frequency",
    ):
        assert CATALOG.metric(name) is None, name
    listed = next(
        item for item in CATALOG.inventory_categories if item.category_id == "shoppers_and_reach"
    )
    assert set(FREQUENCY_METRICS) <= set(listed.metric_ids)


def _roster(period: DateRange) -> Roster:
    return Roster(
        RosterKind.AD_PRODUCT,
        period,
        US,
        "amazon_ads",
        (RosterEntry("sponsored_products", "Sponsored Products"),),
    )


def _plan(metric: str, grains: tuple[str, ...], filters: Mapping[str, str]):
    channel = filters.get("grain_set") == "channel"
    return plan(
        CATALOG,
        PlanRequest(
            metric,
            grains,
            AUGUST.window,
            {
                "marketplace_key": US,
                "time_grain": "month",
                "row_kind": "window",
                "bucket_scheme": "fine",
                **filters,
            },
            roster=_roster(AUGUST.window) if channel else None,
        ),
        FactShape.for_fact(FACT, CONTRACTS),
    )


def test_return_by_frequency_is_refused_per_channel_because_it_uses_credited_revenue() -> None:
    """Design §5.5 (review DS6-2): any-ad sales over one channel's spend isn't that channel's."""
    for grain in ("ad_product", "channel"):
        refused = CATALOG.refusal_for(
            "roas_incl_halo_by_frequency",
            grain=grain,
            filters={"identity_space": "account", "time_grain": "month"},
        )
        assert refused is not None
        assert refused.reason is RefusalReason.GRAIN_NOT_SUPPORTED
        assert "per-channel return uses credited revenue" in refused.explanation
    # Paired controls: the platform grain isn't refused for that reason, and per ad product each
    # bucket's own figures (what the Reach bake reads) plan.
    assert (
        CATALOG.refusal_for(
            "roas_incl_halo_by_frequency",
            grain="frequency_bucket",
            filters={"identity_space": "account", "time_grain": "month"},
        )
        is None
    )
    planned = _plan(
        "shoppers_in_frequency_bucket",
        ("marketplace", "ad_product", "frequency_bucket", "identity_space"),
        {"grain_set": "channel"},
    )
    assert planned.measures == ("shoppers",)
    assert planned.roster == _roster(AUGUST.window)


def test_no_query_adds_the_two_window_spends_so_return_by_frequency_is_baked_only() -> None:
    """Their currencies are their own (B2-5): only the Reach bake adds them, where they agree."""
    grains = ("marketplace", "frequency_bucket", "identity_space")
    for metric in ("window_spend_in_frequency_bucket", "roas_incl_halo_by_frequency"):
        with pytest.raises(DomainError, match="cannot compute"):
            _plan(metric, grains, {"grain_set": "platform", "identity_space": "account"})
    # Control: each window spend plans on its own, labelled from its own currency column.
    dsp = _plan("dsp_window_spend_in_frequency_bucket", grains, {"grain_set": "platform"})
    sponsored = _plan(
        "sponsored_window_spend_in_frequency_bucket", grains, {"grain_set": "platform"}
    )
    assert (dsp.measures, dsp.currency_source) == (("dsp_window_spend",), "dsp_currency")
    assert (sponsored.measures, sponsored.currency_source) == (
        ("sponsored_window_spend",),
        "sponsored_currency",
    )


def test_purchases_are_never_summed_across_the_ad_products_of_one_delivery() -> None:
    """D-9: one purchase sits in each ad product's row, so a sum across them double counts."""
    with pytest.raises(DomainError, match="ad_product_key"):
        _plan(
            "purchases_in_frequency_bucket",
            ("marketplace", "frequency_bucket", "identity_space"),
            {"grain_set": "channel"},
        )
    # Paired control: per ad product, each bucket reads its own purchases.
    planned = _plan(
        "purchases_in_frequency_bucket",
        ("marketplace", "ad_product", "frequency_bucket", "identity_space"),
        {"grain_set": "channel"},
    )
    assert planned.measures == ("purchases",)


# --- chat: one scheme per comparison (D-8 (a)) ---------------------------------------------


@dataclass
class _Rows:
    seen: list[QueryRequest]

    def run(self, request: QueryRequest) -> QueryResult:
        self.seen.append(request)
        plan_ = request.plan.logical
        rows: list[QueryRow] = []
        for bucket in FINE:
            row: dict[str, object] = {
                "marketplace_key": US,
                "frequency_bucket": bucket,
                "identity_space": "account",
                measure_alias(0): Decimal(1000),
            }
            rows.append(cast("QueryRow", {k: row[k] for k in (*plan_.group_by, measure_alias(0))}))
        return QueryResult(
            rows=tuple(rows), bytes_scanned=1, truncated=False, suppression=Suppression(False)
        )


@dataclass
class _Served:
    series: dict[CalendarPeriod, tuple[str | None, ...]]

    def served_periods(self, request: ServedPeriodsRequest) -> ServedPeriods:
        published = tuple(contract.contract_id for contract in request.contracts)
        return ServedPeriods(
            frozenset((contract, period) for contract in published for period in self.series),
            dict.fromkeys(published, 16),
            {
                (contract, period): held
                for contract in published
                for period, held in self.series.items()
            },
        )


@dataclass
class _Rosters:
    def roster(self, request: RosterRequest) -> Roster:
        return Roster(
            request.kind,
            request.period,
            request.marketplace_key,
            request.platform_key,
            (RosterEntry("sponsored_products", "Sponsored Products"),),
        )


def _chat(series: dict[CalendarPeriod, tuple[str | None, ...]], *, prior: bool) -> _Rows:
    queries = _Rows([])
    handle(
        MeasurementToolRequest(
            tool=MeasurementTool.METRIC_BY_GRAIN,
            catalog=CATALOG,
            contracts=CONTRACTS,
            metric_name="shoppers_in_frequency_bucket",
            grains=("marketplace", "frequency_bucket", "identity_space"),
            window=SEPTEMBER.window,
            prior_window=AUGUST.window if prior else None,
            computed_at=NOW,
            correlation_id="corr-frequency-scheme",
            filters={"marketplace_key": US},
        ),
        MeasurementTooling(
            queries=queries,
            publications=StubPublications(),
            periods=_Served(series),
            rosters=_Rosters(),
        ),
    )
    return queries


def test_chat_refuses_a_comparison_across_bucket_schemes() -> None:
    with pytest.raises(ValidationError) as caught:
        _chat({SEPTEMBER: ("coarse",), AUGUST: (None,)}, prior=True)
    error = caught.value
    assert error.description == "PACK_VARIANT_MISMATCH: bucket scheme changed in September 2026"
    assert error.context["reason"] == "bucket_scheme_changed"
    assert error.context["finding_code"] == "PACK_VARIANT_MISMATCH"
    # Paired control: two coarse months compare, each read from its window rows.
    queries = _chat({SEPTEMBER: ("coarse",), AUGUST: ("coarse",)}, prior=True)
    assert {request.plan.logical.filters["row_kind"] for request in queries.seen} == {"window"}
    assert len(queries.seen) == 2


def test_a_series_reads_its_own_scheme_window_rows() -> None:
    fine = _chat({SEPTEMBER: (None,)}, prior=False)
    assert [request.plan.logical.filters["row_kind"] for request in fine.seen] == ["window"]
    # The platform grain set is read: no ad product is named (D-1).
    assert fine.seen[0].plan.logical.filters["grain_set"] == "platform"
    assert fine.seen[0].plan.logical.filters["bucket_scheme"] == "fine"
    coarse = _chat({SEPTEMBER: ("coarse",)}, prior=False)
    assert [request.plan.logical.filters["row_kind"] for request in coarse.seen] == ["window"]
    assert coarse.seen[0].plan.logical.filters["bucket_scheme"] == "coarse"
    assert DateRange(SEPTEMBER.start, SEPTEMBER.end) == coarse.seen[0].plan.logical.window


# --- money on a shopper-aggregate cell with nothing to denominate --------------------------------


def _money_rows(*cells: tuple[str, object, object]) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "marketplace_key": US,
            "frequency_bucket": bucket,
            "identity_space": "account",
            "currency_code": currency,
            measure_alias(0): value,
        }
        for bucket, value, currency in cells
    )


def test_a_not_returned_or_untouched_cell_names_no_currency_and_needs_none() -> None:
    """A gap (NULL) or a bucket no DSP touch reached (0) has no money to label (ADR-099 pt 8)."""
    grains = ("marketplace", "frequency_bucket", "identity_space")
    revenue = _plan("revenue_incl_halo_in_frequency_bucket", grains, {"grain_set": "platform"})
    read = measure(revenue, _money_rows(("1", Decimal("1950.0000"), "USD"), ("9", None, None)))  # type: ignore[arg-type]
    assert read.units == Units(kind=UnitKind.CURRENCY, currency_code="USD")
    assert [figure.value for figure in read.figures] == [Decimal("1950.0000")]
    assert read.undefined_rows == 1
    dsp = _plan("dsp_window_spend_in_frequency_bucket", grains, {"grain_set": "platform"})
    spent = measure(dsp, _money_rows(("1", Decimal("6.0000"), "USD"), ("2", Decimal(0), None)))  # type: ignore[arg-type]
    assert spent.units == Units(kind=UnitKind.CURRENCY, currency_code="USD")
    assert [figure.value for figure in spent.figures] == [Decimal("6.0000"), Decimal(0)]
    # Paired control: an amount that came back without its currency is still refused.
    with pytest.raises(InvariantError, match="does not say what currency"):
        measure(dsp, _money_rows(("1", Decimal("6.0000"), "USD"), ("2", Decimal(3), None)))  # type: ignore[arg-type]


# --- the fixture's landing runs ------------------------------------------------------------


def _runs(contract: str) -> dict[tuple[str, str], dict[str, object]]:
    fixture = f"generated/{contract}.csv"
    shape = load_synthetic_entity_catalog(ROOT).shape
    runs = _periodic_runs(
        _SourceRunsRequest(
            fixture,
            (FIXTURES / f"{contract}.csv").read_text(),
            json.loads((FIXTURES / f"{contract}.manifest.json").read_text()),
            30,
            shape,
            16,
        )
    )
    return {
        (str(declaration["period_start"]), str(declaration["period_end"])): cast(
            "dict[str, object]", declaration["option_set"]
        )
        for _name, _text, declaration, _day in runs
    }


@pytest.mark.parametrize("contract", (PLATFORM, CHANNEL))
def test_the_month_after_the_degraded_one_lands_as_the_coarse_variant(contract: str) -> None:
    runs = _runs(contract)
    # The month after the shaped withheld month (August) names the coarse variant (plan D10).
    assert runs["2026-09-01", "2026-09-30"] == {"period_grain": "month", "pack_variant": "coarse"}
    # Controls: the degraded month itself and every quarter stay on the base pack.
    assert runs["2026-08-01", "2026-08-31"] == {"period_grain": "month"}
    assert runs["2026-07-01", "2026-09-30"] == {"period_grain": "quarter"}
    # The NULL-bucket month (June) is refused by the load, so it is never landed.
    assert ("2026-06-01", "2026-06-30") not in runs
    assert ("2026-05-01", "2026-05-31") in runs


def test_a_delivery_mixing_bucket_schemes_is_refused_at_landing() -> None:
    with pytest.raises(LandingError, match="bucket schemes"):
        _variant_options(
            ("bucket_scheme",), [{"bucket_scheme": "fine"}, {"bucket_scheme": "coarse"}]
        )
    # Control: a column-less corpus names no variant.
    assert _variant_options(("shoppers",), [{"shoppers": "1"}]) == {}
