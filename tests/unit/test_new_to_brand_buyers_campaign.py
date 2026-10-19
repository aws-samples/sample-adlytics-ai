# SPDX-License-Identifier: MIT-0
"""Pack 3: new buyers per campaign (`amc-new-to-brand-buyers-campaign`), chat-only.

The campaign grain set of `fct_new_to_brand_buyers`: one row per campaign per calendar month, its
own extraction (D-1), never added across campaigns (D-2), read against the campaigns with spend
in the same month (design §4.5). A roster of 20 or more with more than a fifth missing withdraws
the grain set: no campaign figure is offered and each reads "not available" with the pointer to
new buyers per channel (design §4.4 case 3). Every figure is fictional and synthetic. The load
tests run the real ingestion path on in-memory tooling; the chat tests the real planner and tool.
"""

from __future__ import annotations

import csv
import io
import json
import pathlib
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from publication_support import StubPublications
from test_dimension_labels import (
    SCOPE,
    _LabelCatalog,  # pyright: ignore[reportPrivateUsage]
)
from test_new_to_brand_buyers import (
    _Current,  # pyright: ignore[reportPrivateUsage]
)
from test_new_to_brand_buyers_channel import FACT_COLUMNS
from test_semantic_sidecar import (
    MemoryCanonical,
    _case,  # pyright: ignore[reportPrivateUsage]
)

from adapters.inbound.tool_handlers.dimension_labels import DimensionLabelResolver
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
from adapters.jobs.ingest_delivery.semantic_sidecar import SemanticSidecarOutcome
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import PublicationSelector
from adapters.outbound.source_pack import load_source_packs
from adapters.outbound.spend_roster import SpendRosterReader
from adapters.producer.packs import discard_policies, load_query_packs, pack_by_id
from domain.catalog.capability import GRAIN_NOT_SUPPORTED_CODE, Catalog
from domain.curation.landing import GOVERNED_MANIFEST_LAYOUT, LandedObject
from domain.data_contracts.contract import DataContract
from domain.measurement.planning import (
    FactShape,
    FactShapes,
    PlanRequest,
    measure_alias,
    plan,
)
from domain.measurement.rosters import (
    ROSTER_REFUSED,
    ROSTER_UNAVAILABLE,
    ROSTER_WITHDRAWN,
    SERVED_GRAIN_SETS,
    PinnedKey,
    Roster,
    RosterEntry,
    RosterKind,
    apply_pinned_roster,
    apply_roster,
    coarser_grain_set,
    roster_lines,
)
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.identity import (
    CanonicalCampaignKeyRequest,
    ChannelKey,
    PlatformKey,
    SourceIdentifier,
    SourceSystemKey,
    canonical_campaign_key,
)
from domain.semantics.publication import PublicationId, publication_lens_for
from domain.semantics.source_aligned import LandedDeliveryRef
from domain.semantics.time import TimeGrain
from domain.shared_kernel.errors import DomainError, InvariantError, ValidationError
from domain.shared_kernel.provenance import Suppression
from domain.shared_kernel.values import DateRange
from domain.shopper_aggregates.grain_sets import GrainSet
from domain.shopper_aggregates.loss import DEGRADED_NOTE, LossGrade
from ports.outbound.canonical import CanonicalDestination
from ports.outbound.publication_selection import (
    PublicationSelection,
    PublicationSelectionRequest,
)
from ports.outbound.query import QueryRequest, QueryResult
from ports.outbound.rosters import RosterRequest
from ports.outbound.served_periods import ServedPeriods, ServedPeriodsRequest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-099", "ADR-100", "ADR-106")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/generated"
PACK_ID = "amc-new-to-brand-buyers-campaign"
CHANNEL = "amc-new-to-brand-buyers-channel"
PLATFORM = "amc-new-to-brand-buyers-platform"
FACT = "fct_new_to_brand_buyers"
US = "ATVPDKIKX0DER"
NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)
SEPTEMBER = DateRange(date(2026, 9, 1), date(2026, 9, 30))
AUGUST = DateRange(date(2026, 8, 1), date(2026, 8, 31))
LAST_WEEK = DateRange(date(2026, 9, 21), date(2026, 9, 27))
POINTER = "too few shoppers in this breakdown; see new buyers per channel"
LABEL = (
    "New-to-brand ad-attributed buyers (12 month lookback) · 14-day attribution · conversion "
    "date · advertised products"
)
HEADER = (
    "conversion_event_marketplace_name,ad_product_type,campaign_id_string,"
    "buyers_new_to_brand_12mo,buyers_attributed,time_window_start,time_window_end,"
    "distinct_user_count,suppression_reason,rows_suppressed"
)
WINDOW = "2026-09-01T00:00:00.000Z,2026-10-01T00:00:00.000Z"
DSP_ROW = f"AMAZON.COM,amazon_dsp,910000000000000001,150,310,{WINDOW},2,,false"
SP_ROW = f"AMAZON.COM,sponsored_products,A0SYNTHETIC00000001,240,480,{WINDOW},2,,false"
WITHHELD = (
    f",,,1,1,{WINDOW},1,1 amazon_attributed_events_by_conversion_time users < 2 required,true"
)
"""A suppressed row as AMC delivers one live: its LOW keys NULL, counts kept."""


def _csv(*lines: str) -> str:
    return "".join(f"{line}\n" for line in (HEADER, *lines))


@pytest.fixture(scope="module")
def contracts() -> tuple[DataContract, ...]:
    return tuple(contract for _, contract in load_contracts(ROOT))


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_catalog(ROOT)


# --- Pack and contract ----------------------------------------------------------------------------


def test_the_pack_is_pack_2_with_the_campaign_added_monthly_only() -> None:
    campaign, channel = pack_by_id(ROOT, PACK_ID), pack_by_id(ROOT, CHANNEL)
    coalesced = "COALESCE(ad_product_type, 'amazon_dsp')"
    assert campaign.sql == (
        channel.sql.replace(
            f"{coalesced} AS ad_product_type, ",
            f"{coalesced} AS ad_product_type, campaign_id_string, ",
        ).replace(
            f"GROUP BY conversion_event_marketplace_name, {coalesced}",
            f"GROUP BY conversion_event_marketplace_name, {coalesced}, campaign_id_string",
        )
    )
    assert campaign.expected_columns == (
        *channel.expected_columns[:2],
        "campaign_id_string",
        *channel.expected_columns[2:],
    )
    assert [grain.value for grain in campaign.period_policy.grains] == ["month"]  # type: ignore[union-attr]
    assert [grain.value for grain in channel.period_policy.grains] == ["week", "month"]  # type: ignore[union-attr]
    assert (
        campaign.expected_bar,
        campaign.effective_cadence_lag_days,
        campaign.input_parameters,
        campaign.suppression_policy,
    ) == (
        channel.expected_bar,
        channel.effective_cadence_lag_days,
        channel.input_parameters,
        channel.suppression_policy,
    )
    assert any(item.contract_id == PACK_ID for item in discard_policies(load_query_packs(ROOT)))


def test_the_generated_tables_add_one_source_aligned_table_and_no_fact_column() -> None:
    generated = json.loads((ROOT / "contracts/generated/v2-tables.json").read_text())
    source = {table["name"]: table for table in generated["sourceAlignedTables"]}
    assert source["sa_amc_new_to_brand_buyers_campaign"]["partitionKeys"] == [
        "source_publication_id"
    ]
    for layer in ("canonicalCandidateTables", "canonicalCurrentTables"):
        tables = {table["name"]: table for table in generated[layer]}
        fact = tables[FACT]
        assert fact["writtenBy"] == [PACK_ID, CHANNEL, PLATFORM]
        # The 27 columns pack 1 created, unchanged in name, field id, type and nullability.
        assert [
            (column["name"], column["fieldId"], column["type"], column["nullable"])
            for column in fact["columns"]
        ] == [(name, index, *kind) for index, (name, *kind) in enumerate(FACT_COLUMNS, start=1)]
        assert "campaign_key" in fact["grain"]
        # Distinct counts and identity columns only: no residual table for this fact.
        assert f"{FACT}_withheld" not in tables


# --- Load: discard, no closed enumeration, campaign identity --------------------------------------


def _delivery(text: str, window: DateRange = SEPTEMBER) -> LoadedDelivery:
    body = text.encode()
    declaration = json.loads((FIXTURES / f"{PACK_ID}.manifest.json").read_text())
    manifest = build_manifest(
        BuildManifestRequest(
            declaration={
                **declaration,
                "period_start": window.start.isoformat(),
                "period_end": window.end.isoformat(),
                "source_watermark": (window.end + timedelta(days=2)).isoformat(),
            },
            objects=(LandedObject("data-0001.csv", len(body), checksum_of(body)),),
            declared_columns=declared_columns_of(text, source_format="csv"),
        )
    )
    manifest = replace(
        manifest,
        governance=pack_by_id(ROOT, PACK_ID).governance,
        layout_version=GOVERNED_MANIFEST_LAYOUT,
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


@dataclass
class _Loaded:
    outcome: SemanticSidecarOutcome
    rows: tuple[dict[str, object], ...]
    loss: str | None
    contract_rule: tuple[str, str] = ("", "")


def _load(delivery: LoadedDelivery) -> _Loaded:
    _request, tooling, _order, _attempts = _case()
    results = ingest_semantic_delivery(
        SemanticIngestionRequest(
            delivery=delivery,
            contracts=tuple(
                contract for _, contract in load_contracts(ROOT) if contract.contract_id == PACK_ID
            ),
            columns=load_registry(ROOT),
            now=NOW,
            operation_id="ntb-campaign-unit",
            execution_id="ntb-campaign-execution",
            plan_digest="sha256:ntb-campaign-plan",
            installed_governance=(pack_by_id(ROOT, PACK_ID).governance,),
            discard_policies=discard_policies(load_query_packs(ROOT)),
        ),
        tooling,
    )
    canonical = tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    candidates = tuple(
        item
        for item in canonical.requests
        if item.destination is CanonicalDestination.CANDIDATE
        and item.publication.partition.scope.dataset.value == FACT
    )
    return _Loaded(
        outcome=results[0].outcome,
        rows=tuple(dict(row) for item in candidates for row in item.rows),
        loss=next(
            (
                result.detail
                for item in candidates
                for result in item.publication.evidence.results
                if result.rule_id == f"suppression:{PACK_ID}.cell-loss.v1"
            ),
            None,
        ),
        contract_rule=next(
            (
                (result.outcome.value, result.detail)
                for item in candidates
                for result in item.publication.evidence.results
                if result.rule_id == f"contract.{PACK_ID}.v1"
            ),
            ("", ""),
        ),
    )


def _campaign_key(channel: str, source_value: str) -> str:
    """Return the canonical key every campaign-bearing pack derives, spend rows too (ADR-053)."""
    return canonical_campaign_key(
        CanonicalCampaignKeyRequest(
            commerce_scope=load_commerce_identity_bridge(ROOT).commerce_scope,
            platform=PlatformKey("amazon_ads"),
            channel=ChannelKey(channel),
            source_system=SourceSystemKey("amc"),
            source_scope=SourceIdentifier("amc.entity", "fictional-fixture-amc-entity"),
            identifier_kind="campaign_id_string",
            source_value=source_value,
        )
    ).value


_CAMPAIGN_ROW = {
    "basis_key": "ad_attributed",
    "ecosystem_key": "amazon",
    "grain_set": "campaign",
    "identity_unit": "amazon_user_id",
    "marketplace_key": "amazon_us",
    "platform_key": "amazon_ads",
    "source_timezone": "UTC",
    "cell_state": "returned",
    "period_end": SEPTEMBER.end,
    "period_start": SEPTEMBER.start,
    "time_grain": "month",
}


def test_returned_campaigns_publish_with_their_ad_product_and_a_withheld_one_is_dropped() -> None:
    loaded = _load(_delivery(_csv(DSP_ROW, SP_ROW, WITHHELD)))
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED
    assert sorted(loaded.rows, key=lambda row: str(row["ad_product_key"])) == [
        {
            **_CAMPAIGN_ROW,
            "campaign_key": _campaign_key("amazon_dsp", "910000000000000001"),
            "ad_product_key": "amazon_dsp",
            "channel_key": "amazon_dsp",
            "channel_group_key": "display",
            "buyers_attributed": 310,
            "buyers_new_to_brand_12mo": 150,
        },
        {
            **_CAMPAIGN_ROW,
            "campaign_key": _campaign_key("amazon_sponsored_products", "A0SYNTHETIC00000001"),
            "ad_product_key": "sponsored_products",
            "channel_key": "amazon_sponsored_products",
            "channel_group_key": "search",
            "buyers_attributed": 480,
            "buyers_new_to_brand_12mo": 240,
        },
    ]
    # No closed enumeration at load: each returned campaign is its own cell. The roster grades
    # the loss at serve.
    assert loaded.loss == (
        '{"discarded_rows":1,"expected_cells":2,"grade":"not_degraded",'
        '"loss_ratio":"0","not_returned_cells":0}'
    )


def test_a_week_delivery_never_loads_as_a_campaign_month() -> None:
    """The source pack declares months only, so a week's window has no bucket (control above)."""
    week = "2026-09-21T00:00:00.000Z,2026-09-28T00:00:00.000Z"
    row = f"AMAZON.COM,amazon_dsp,910000000000000001,150,310,{week},2,,false"
    with pytest.raises(ValueError, match="is not one complete month period"):
        _load(_delivery(_csv(row), LAST_WEEK))


def test_a_repeated_campaign_rejects_the_delivery() -> None:
    blocked = _load(_delivery(_csv(SP_ROW, SP_ROW)))
    assert blocked.outcome is SemanticSidecarOutcome.BLOCKED
    assert blocked.contract_rule[0] == "fail"
    assert "CELL_DUPLICATE: row 1 repeats cell campaign_key=" in blocked.contract_rule[1]
    # Positive control: two different campaigns publish.
    assert _load(_delivery(_csv(SP_ROW, DSP_ROW))).outcome is SemanticSidecarOutcome.PUBLISHED


# --- Planner: month only, D-2 no sum across campaigns, D-6 spend days -----------------------------


def _filters(grain: str = "month") -> dict[str, str]:
    return {"time_grain": grain, "grain_set": "campaign", "marketplace_key": US}


def _roster(window: DateRange, size: int) -> Roster:
    return Roster(
        RosterKind.CAMPAIGN,
        window,
        US,
        "amazon_ads",
        tuple(
            RosterEntry(f"campaign:c{index:02d}", f"Campaign {index:02d}") for index in range(size)
        ),
    )


def _refused(call: object) -> DomainError:
    with pytest.raises(DomainError) as refused:
        call()  # type: ignore[operator]
    return refused.value


def test_a_campaign_request_plans_at_grain_set_campaign_for_a_month_and_refuses_a_week(
    catalog: Catalog, contracts: tuple[DataContract, ...]
) -> None:
    shape = FactShape.for_fact(FACT, contracts)
    roster = _roster(SEPTEMBER, 5)
    month = plan(
        catalog,
        PlanRequest(
            "buyers_new_to_brand_12mo", ("campaign",), SEPTEMBER, _filters(), roster=roster
        ),
        shape,
    )
    assert (month.group_by, month.roster) == (("campaign_key",), roster)
    # The campaign grain set delivers calendar months only, so a week names the nearest month.
    week = _refused(
        lambda: plan(
            catalog,
            PlanRequest(
                "buyers_new_to_brand_12mo",
                ("campaign",),
                LAST_WEEK,
                _filters("week"),
                roster=_roster(LAST_WEEK, 5),
            ),
            shape,
        )
    )
    assert (week.code, week.description) == (
        GRAIN_NOT_SUPPORTED_CODE,
        "buyers_new_to_brand_12mo per campaign is measured for complete months only, not per "
        "week. The nearest complete month is August 2026 (2026-08-01 to 2026-08-31).",
    )
    assert week.context["reason"] == "period_grain_not_declared"
    # Control: the same week per ad product plans, because the channel grain set has weeks.
    assert plan(
        catalog,
        PlanRequest(
            "buyers_new_to_brand_12mo",
            ("ad_product",),
            LAST_WEEK,
            {**_filters("week"), "grain_set": "channel"},
            roster=replace(_roster(LAST_WEEK, 0), kind=RosterKind.AD_PRODUCT),
        ),
        shape,
    ).group_by == ("ad_product_key",)


def test_a_sum_across_campaigns_is_refused_beside_the_per_campaign_figure(
    catalog: Catalog, contracts: tuple[DataContract, ...]
) -> None:
    shape = FactShape.for_fact(FACT, contracts)
    roster = _roster(SEPTEMBER, 5)
    error = _refused(
        lambda: plan(
            catalog,
            PlanRequest(
                "buyers_new_to_brand_12mo", ("marketplace",), SEPTEMBER, _filters(), roster=roster
            ),
            shape,
        )
    )
    assert error.code == GRAIN_NOT_SUPPORTED_CODE
    assert error.description.startswith(
        "buyers_new_to_brand_12mo over fct_new_to_brand_buyers would sum across campaign_key, "
    )
    # Per ad product at the campaign grain set would add campaigns too.
    error = _refused(
        lambda: plan(
            catalog,
            PlanRequest("buyers_attributed", ("ad_product",), SEPTEMBER, _filters(), roster=roster),
            shape,
        )
    )
    assert "would sum across campaign_key" in error.description
    # Positive control: per campaign, with or without its ad product, plans.
    assert plan(
        catalog,
        PlanRequest(
            "buyers_attributed", ("ad_product", "campaign"), SEPTEMBER, _filters(), roster=roster
        ),
        shape,
    ).group_by == ("ad_product_key", "campaign_key")


def test_a_campaign_figure_without_its_roster_is_refused(
    catalog: Catalog, contracts: tuple[DataContract, ...]
) -> None:
    shape = FactShape.for_fact(FACT, contracts)
    request = PlanRequest("buyers_new_to_brand_12mo", ("campaign",), SEPTEMBER, _filters())
    with pytest.raises(InvariantError) as refused:
        plan(catalog, request, shape)
    assert (refused.value.code, refused.value.context) == (
        ROSTER_UNAVAILABLE,
        {"roster": "campaign", "window": "2026-09-01..2026-09-30"},
    )
    # An ad-product roster isn't a campaign roster; the campaign one plans.
    with pytest.raises(InvariantError):
        plan(
            catalog,
            replace(request, roster=replace(_roster(SEPTEMBER, 2), kind=RosterKind.AD_PRODUCT)),
            shape,
        )
    assert plan(catalog, replace(request, roster=_roster(SEPTEMBER, 2)), shape).roster is not None


def test_d6_cost_per_new_buyer_per_campaign_needs_every_spend_day_of_the_month(
    catalog: Catalog, contracts: tuple[DataContract, ...]
) -> None:
    logical = plan(
        catalog,
        PlanRequest(
            "cost_per_new_to_brand_buyer_12mo",
            ("campaign",),
            SEPTEMBER,
            _filters(),
            roster=_roster(SEPTEMBER, 1),
        ),
        FactShapes(
            numerator=FactShape.for_fact("fct_ad_exposure", contracts),
            denominator=FactShape.for_fact(FACT, contracts),
        ),
    )
    assert logical.group_by == ("campaign_key",)
    selector = PublicationSelector(
        _Current(), load_source_packs(ROOT), load_commerce_identity_bridge(ROOT)
    )
    bound = selector.select(PublicationSelectionRequest((logical,), contracts, NOW))
    spend_days = {
        item.partition.bucket.window.start
        for item in bound.plans[0].primary
        if item.partition.scope.dataset.value == "fct_ad_exposure"
    }
    assert spend_days == {SEPTEMBER.start + timedelta(days=offset) for offset in range(30)}
    # Only the campaign grain set's month publication is bound, never pack 1's or pack 2's.
    pack = next(item for item in load_source_packs(ROOT) if item.contract_id == PACK_ID)
    assert [item.partition.scope.lens for item in bound.plans[0].joined] == [
        publication_lens_for(
            "source_pack.amc_new_to_brand_buyers_campaign",
            (("period_grain", '"month"'),),
            pack.registry_version,
        )
    ]
    missing = next(
        item.partition.identity
        for item in bound.plans[0].primary
        if item.partition.bucket.window.start == date(2026, 9, 15)
    )
    gap = PublicationSelector(
        _Current(absent=frozenset({missing})),
        load_source_packs(ROOT),
        load_commerce_identity_bridge(ROOT),
    )
    with pytest.raises(DomainError) as refused:
        gap.select(PublicationSelectionRequest((logical,), contracts, NOW))
    assert refused.value.description.startswith("No published data covers the requested window")


def test_the_campaign_grain_set_points_to_new_buyers_per_channel() -> None:
    """Design §4.4: pack 3 campaign → pack 2 per channel → pack 1 platform."""
    assert coarser_grain_set(FACT, "campaign") == GrainSet("channel", "new buyers per channel")
    assert [item.name for item in SERVED_GRAIN_SETS[FACT].grain_sets] == [
        "platform",
        "channel",
        "campaign",
    ]


# --- Chat: the roster in the answer, withdrawal and the month rule --------------------------------


@dataclass
class _Rows:
    rows: tuple[dict[str, object], ...]
    seen: list[QueryRequest] = field(default_factory=list[QueryRequest])

    def run(self, request: QueryRequest) -> QueryResult:
        self.seen.append(request)
        return QueryResult(
            rows=self.rows,  # type: ignore[arg-type]
            bytes_scanned=1,
            truncated=False,
            suppression=Suppression(rows_suppressed=False),
        )


_MONTHS = (
    CalendarPeriod(AUGUST.start, AUGUST.end, TimeGrain.MONTH),
    CalendarPeriod(SEPTEMBER.start, SEPTEMBER.end, TimeGrain.MONTH),
)


@dataclass
class _Served:
    """Every contract published August and September; only the weekly ones a week too."""

    def served_periods(self, request: ServedPeriodsRequest) -> ServedPeriods:
        published = {contract.contract_id for contract in request.contracts}
        week = CalendarPeriod(LAST_WEEK.start, LAST_WEEK.end, TimeGrain.WEEK)
        return ServedPeriods(
            frozenset(
                {
                    *((contract_id, month) for contract_id in published for month in _MONTHS),
                    *((contract_id, week) for contract_id in published if contract_id != PACK_ID),
                }
            ),
            dict.fromkeys(published, 2),
        )


@dataclass
class _Rosters:
    size: int
    filtered: int | None = None
    """The roster's size under a media filter: the campaigns with spend on that ad product."""
    seen: list[RosterRequest] = field(default_factory=list[RosterRequest])

    def roster(self, request: RosterRequest) -> Roster:
        self.seen.append(request)
        size = self.size if not request.filters or self.filtered is None else self.filtered
        return replace(_roster(request.period, size), kind=request.kind, filters=request.filters)


def _campaign_rows(*indices: int) -> tuple[dict[str, object], ...]:
    return tuple(
        {"campaign_key": f"campaign:c{index:02d}", measure_alias(0): 100 + index}
        for index in indices
    )


def _request(  # noqa: PLR0913 — the question's parts, each defaulted to "by campaign last month"
    contracts: tuple[DataContract, ...],
    *,
    window: DateRange = SEPTEMBER,
    grains: tuple[str, ...] = ("campaign",),
    tool: MeasurementTool = MeasurementTool.METRIC_BY_GRAIN,
    prior_window: DateRange | None = None,
    n: int = 0,
    filters: dict[str, str] | None = None,
) -> MeasurementToolRequest:
    return MeasurementToolRequest(
        tool=tool,
        catalog=load_catalog(ROOT),
        contracts=contracts,
        metric_name="buyers_new_to_brand_12mo",
        grains=grains,
        window=window,
        computed_at=NOW,
        correlation_id="corr-ntb-campaign",
        filters={"marketplace_key": US, **(filters or {})},
        prior_window=prior_window,
        n=n,
    )


@dataclass
class _ByCampaign(_Rows):
    """The delivery's rows: a query pinning a campaign reads that campaign's row alone."""

    def run(self, request: QueryRequest) -> QueryResult:
        pinned = request.plan.logical.filters.get("campaign_key")
        result = super().run(request)
        if pinned is None:
            return result
        return replace(
            result, rows=tuple(row for row in self.rows if row["campaign_key"] == pinned)
        )


def _chat(
    request: MeasurementToolRequest,
    rows: tuple[dict[str, object], ...] | _Rows,
    rosters: _Rosters,
) -> tuple[object, _Rows]:
    queries = rows if isinstance(rows, _Rows) else _Rows(rows)
    response = handle(
        request,
        MeasurementTooling(
            queries=queries, publications=StubPublications(), periods=_Served(), rosters=rosters
        ),
    )
    return response, queries


def _narrative(response: object) -> tuple[str, ...]:
    return cast("tuple[str, ...]", response.narrative)  # type: ignore[attr-defined]


def test_new_buyers_by_campaign_last_month_names_a_missing_campaign_of_5_graded(
    contracts: tuple[DataContract, ...],
) -> None:
    """A 5-campaign roster missing one (20% loss) is graded, never withdrawn (design §4.4)."""
    rosters = _Rosters(5)
    response, queries = _chat(_request(contracts), _campaign_rows(0, 1, 2, 3), rosters)
    assert dict(queries.seen[0].plan.logical.filters) == {
        "grain_set": "campaign",
        "marketplace_key": US,
        "time_grain": "month",
    }
    assert [(item.kind, item.period, item.marketplace_key) for item in rosters.seen] == [
        (RosterKind.CAMPAIGN, SEPTEMBER, US)
    ]
    narrative = _narrative(response)
    assert "Campaign 04 is not available: too few shoppers, or none." in narrative
    assert DEGRADED_NOTE in narrative
    assert [row["campaign_key"] for row in response.data.values] == [  # type: ignore[attr-defined]
        "campaign:c00",
        "campaign:c01",
        "campaign:c02",
        "campaign:c03",
    ]
    # Control: the full roster returned names none and isn't degraded.
    whole, _ = _chat(_request(contracts), _campaign_rows(0, 1, 2, 3, 4), _Rosters(5))
    assert not any("is not available" in line for line in _narrative(whole))
    assert DEGRADED_NOTE not in _narrative(whole)
    assert len(whole.data.values) == 5  # type: ignore[attr-defined]


def test_a_24_campaign_roster_at_25_percent_loss_is_withdrawn_pointing_to_per_channel(
    contracts: tuple[DataContract, ...],
) -> None:
    """Design §4.4 case 3: no campaign figure is offered; every campaign reads not available."""
    response, _ = _chat(_request(contracts), _campaign_rows(*range(18)), _Rosters(24))
    narrative = _narrative(response)
    assert response.data.values == ()  # type: ignore[attr-defined]
    assert narrative[0] == (
        f"{LABEL} for 30 day(s), 2026-09-01 to 2026-09-30, broken down by campaign, isn't "
        f"offered: {POINTER}."
    )
    assert [line for line in narrative if line.startswith("Campaign ")] == [
        f"Campaign {index:02d} is not available: {POINTER}." for index in range(24)
    ]
    assert not any("No new-to-brand" in line for line in narrative)
    # Control: 4 of 24 missing (16.7%) is only degraded and serves the 20 returned campaigns.
    degraded, _ = _chat(_request(contracts), _campaign_rows(*range(20)), _Rosters(24))
    assert len(degraded.data.values) == 20  # type: ignore[attr-defined]
    assert DEGRADED_NOTE in _narrative(degraded)
    assert "Campaign 23 is not available: too few shoppers, or none." in _narrative(degraded)


def test_a_withdrawn_campaign_breakdown_counts_returned_campaigns_off_the_roster() -> None:
    roster = _roster(SEPTEMBER, 24)
    outcome = apply_roster(
        roster, (*roster.keys[:18], "campaign:off-roster"), coarser_grain_set(FACT, "campaign")
    )
    assert (outcome.withdrawn, outcome.loss.grade, outcome.off_roster) == (
        True,
        LossGrade.WITHDRAWN,
        ("campaign:off-roster",),
    )
    assert roster_lines(outcome)[-1] == (
        f"1 more campaign with no ad spend in the period isn't shown either: {POINTER}."
    )


def test_new_buyers_by_campaign_last_week_is_refused_with_the_nearest_month(
    contracts: tuple[DataContract, ...],
) -> None:
    rosters = _Rosters(5)
    with pytest.raises(DomainError) as refused:
        _chat(_request(contracts, window=LAST_WEEK), _campaign_rows(0), rosters)
    assert (refused.value.code, refused.value.description) == (
        GRAIN_NOT_SUPPORTED_CODE,
        "buyers_new_to_brand_12mo is measured for complete months only, so "
        "2026-09-21 to 2026-09-27 can't be answered. The latest measured month is September "
        "2026.",
    )
    assert rosters.seen == []
    # Control: the same week per ad product is answered, at grain set channel.
    response, queries = _chat(
        _request(contracts, window=LAST_WEEK, grains=("ad_product",)),
        ({"ad_product_key": "sponsored_products", measure_alias(0): 9},),
        _Rosters(0),
    )
    assert queries.seen[0].plan.logical.filters["grain_set"] == "channel"
    assert queries.seen[0].plan.logical.filters["time_grain"] == "week"
    assert len(response.data.values) == 1  # type: ignore[attr-defined]


def test_a_comparison_or_ranking_over_a_withdrawn_month_is_refused_with_the_pointer(
    contracts: tuple[DataContract, ...],
) -> None:
    compare = _request(contracts, tool=MeasurementTool.COMPARE_WINDOWS, prior_window=AUGUST)
    with pytest.raises(ValidationError) as refused:
        _chat(compare, _campaign_rows(*range(18)), _Rosters(24))
    assert (refused.value.code, refused.value.description) == (
        ROSTER_WITHDRAWN,
        f"buyers_new_to_brand_12mo per campaign isn't offered for 2026-09-01 to 2026-09-30: "
        f"{POINTER}.",
    )
    top = _request(contracts, tool=MeasurementTool.TOP_N, n=3)
    with pytest.raises(ValidationError) as ranked:
        _chat(top, _campaign_rows(*range(18)), _Rosters(24))
    assert ranked.value.code == ROSTER_WITHDRAWN
    # Control: a graded month ranks, naming the missing campaign.
    response, _ = _chat(top, _campaign_rows(0, 1, 2, 3), _Rosters(5))
    assert "Campaign 04 is not available: too few shoppers, or none." in _narrative(response)


_SPONSORED = {"ad_product_key": "sponsored_products"}


def test_a_breakdown_filtered_to_one_ad_product_is_graded_on_that_ad_products_campaigns(
    contracts: tuple[DataContract, ...],
) -> None:
    """Review A1-1: the roster is the filtered population, so 8 of 8 serve; 18 of 24 withdraws."""
    rosters = _Rosters(24, filtered=8)
    response, _ = _chat(_request(contracts, filters=_SPONSORED), _campaign_rows(*range(8)), rosters)
    assert [item.filters for item in rosters.seen] == [(("ad_product_key", "sponsored_products"),)]
    assert len(response.data.values) == 8  # type: ignore[attr-defined]
    narrative = _narrative(response)
    assert not any("is not available" in line or "isn't offered" in line for line in narrative)
    assert DEGRADED_NOTE not in narrative
    # Control: the same month unfiltered, 18 of the 24 campaigns with spend, is withdrawn.
    whole, _ = _chat(_request(contracts), _campaign_rows(*range(18)), _Rosters(24, filtered=8))
    assert whole.data.values == ()  # type: ignore[attr-defined]
    assert _narrative(whole)[0].endswith(f"isn't offered: {POINTER}.")


def test_a_filtered_graded_roster_never_names_a_campaign_the_filter_excludes(
    contracts: tuple[DataContract, ...],
) -> None:
    response, _ = _chat(
        _request(contracts, filters=_SPONSORED), _campaign_rows(0, 1), _Rosters(5, filtered=2)
    )
    narrative = _narrative(response)
    assert [line for line in narrative if "is not available" in line] == []
    assert DEGRADED_NOTE not in narrative
    # Control: unfiltered, the same two returned campaigns leave Campaigns 02-04 not available.
    whole, _ = _chat(_request(contracts), _campaign_rows(0, 1), _Rosters(5, filtered=2))
    assert [line for line in _narrative(whole) if "is not available" in line] == [
        f"Campaign {index:02d} is not available: too few shoppers, or none." for index in (2, 3, 4)
    ]


def test_a_roster_read_under_other_media_filters_is_refused(
    catalog: Catalog, contracts: tuple[DataContract, ...]
) -> None:
    shape = FactShape.for_fact(FACT, contracts)
    request = PlanRequest(
        "buyers_new_to_brand_12mo",
        ("campaign",),
        SEPTEMBER,
        {**_filters(), **_SPONSORED},
        roster=_roster(SEPTEMBER, 2),
    )
    with pytest.raises(InvariantError) as refused:
        plan(catalog, request, shape)
    assert (refused.value.code, refused.value.description) == (
        ROSTER_UNAVAILABLE,
        "A figure graded against a roster was planned with a roster read under other media "
        "filters.",
    )
    # Control: the roster read under the same filter plans.
    matched = replace(
        request,
        roster=replace(_roster(SEPTEMBER, 2), filters=(("ad_product_key", "sponsored_products"),)),
    )
    assert plan(catalog, matched, shape).roster == matched.roster


def test_one_pinned_campaign_in_a_withdrawn_month_reads_not_available_like_the_breakdown(
    contracts: tuple[DataContract, ...],
) -> None:
    """Review A1-2: withdrawal is per delivery, so the question's shape can't change it."""
    pinned = _request(contracts, filters={"campaign_key": "campaign:c00"})
    queries = _ByCampaign(_campaign_rows(*range(18)))
    response, _ = _chat(pinned, queries, _Rosters(24))
    # The whole delivery is read by campaign first, then the pinned figure.
    assert [request.plan.logical.filters.get("campaign_key") for request in queries.seen] == [
        None,
        "campaign:c00",
    ]
    assert response.data.values == ()  # type: ignore[attr-defined]
    narrative = _narrative(response)
    assert narrative[0] == (
        f"{LABEL} for 30 day(s), 2026-09-01 to 2026-09-30, broken down by campaign, isn't "
        f"offered: {POINTER}."
    )
    assert [line for line in narrative if line.startswith("Campaign ")] == [
        f"Campaign 00 is not available: {POINTER}."
    ]
    # Control: the same pinned campaign in a graded month (4 of 5) serves its figure, alone.
    graded, _ = _chat(pinned, _ByCampaign(_campaign_rows(0, 1, 2, 3)), _Rosters(5))
    assert [row["campaign_key"] for row in graded.data.values] == [  # type: ignore[attr-defined]
        "campaign:c00"
    ]
    assert not any("is not available" in line for line in _narrative(graded))


def test_a_pinned_key_without_its_deliverys_keys_is_refused() -> None:
    roster = _roster(SEPTEMBER, 24)
    coarser = coarser_grain_set(FACT, "campaign")
    pinned = PinnedKey("campaign:c00", answered=True)
    with pytest.raises(InvariantError) as refused:
        apply_pinned_roster(roster, pinned, coarser)
    assert (refused.value.code, refused.value.description) == (
        ROSTER_UNAVAILABLE,
        "A figure for one rostered key was planned without the keys its delivery returned.",
    )
    # Control: with the delivery's keys, 18 of 24 withdraws and names the pinned key alone.
    outcome = apply_pinned_roster(replace(roster, returned=roster.keys[:18]), pinned, coarser)
    assert (outcome.withdrawn, outcome.roster.keys) == (True, ("campaign:c00",))
    assert roster_lines(outcome) == (f"Campaign 00 is not available: {POINTER}.",)


@dataclass
class _Advancing(StubPublications):
    """CURRENT advances between selector calls: call n binds revision n's publication ids."""

    def select(self, request: PublicationSelectionRequest) -> PublicationSelection:
        selection = StubPublications.select(self, request)
        revision = len(self.seen)
        return replace(
            selection,
            plans=tuple(
                replace(
                    item,
                    primary=tuple(
                        replace(
                            publication,
                            publication_id=PublicationId(
                                f"{publication.publication_id.value}-r{revision}"
                            ),
                        )
                        for publication in item.primary
                    ),
                )
                for item in selection.plans
            ),
        )


def test_a_pinned_campaigns_grade_and_figure_read_one_publication_selection(
    contracts: tuple[DataContract, ...],
) -> None:
    """Review B2-1: the delivery's keys and the pinned figure come from one bound selection."""
    publications = _Advancing()
    queries = _ByCampaign(_campaign_rows(*range(18)))
    response = handle(
        _request(contracts, filters={"campaign_key": "campaign:c00"}),
        MeasurementTooling(
            queries=queries, publications=publications, periods=_Served(), rosters=_Rosters(24)
        ),
    )
    assert len(publications.seen) == 1
    delivery, figure = queries.seen
    assert (
        delivery.plan.logical.filters.get("campaign_key"),
        figure.plan.logical.filters.get("campaign_key"),
    ) == (None, "campaign:c00")
    assert delivery.plan.primary_ids == figure.plan.primary_ids
    assert {item.rsplit("-", 1)[1] for item in figure.plan.primary_ids} == {"r1"}
    assert response.data.values == ()  # type: ignore[attr-defined]
    # Control: two selector calls for the same plans bind different publications, so a second
    # selection would have graded the figure on another delivery.
    first = publications.select(publications.seen[0])
    second = publications.select(publications.seen[0])
    assert first.plans[0].primary_ids != second.plans[0].primary_ids


# --- The campaign roster reader: names, never keys ------------------------------------------------


@dataclass
class _Spend:
    rows: tuple[dict[str, object], ...]
    seen: list[QueryRequest] = field(default_factory=list[QueryRequest])

    def run(self, request: QueryRequest) -> QueryResult:
        self.seen.append(request)
        return QueryResult(
            rows=self.rows,  # type: ignore[arg-type]
            bytes_scanned=1,
            truncated=False,
            suppression=Suppression(rows_suppressed=False),
        )


@dataclass
class _Names:
    names: dict[str, str]

    def public_names(
        self, column: str, values: tuple[str, ...], window: DateRange
    ) -> dict[str, str]:
        del column, window
        return {value: self.names[value] for value in values if value in self.names}


_SPEND: tuple[dict[str, object], ...] = (
    {"campaign_key": "campaign:b", "currency_code": "USD", measure_alias(0): Decimal("4.0000")},
    {"campaign_key": "campaign:a", "currency_code": "USD", measure_alias(0): Decimal("9.0000")},
    {"campaign_key": "campaign:z", "currency_code": "USD", measure_alias(0): Decimal("0.0000")},
)


def _reader(
    contracts: tuple[DataContract, ...], names: _Names | DimensionLabelResolver | None
) -> SpendRosterReader:
    return SpendRosterReader(
        catalog=load_catalog(ROOT),
        contracts=contracts,
        publications=PublicationSelector(
            _Current(), load_source_packs(ROOT), load_commerce_identity_bridge(ROOT)
        ),
        queries=_Spend(_SPEND),
        labels={},
        clock=lambda: NOW,
        names=names,
    )


_CAMPAIGNS = RosterRequest(RosterKind.CAMPAIGN, SEPTEMBER, US, "amazon_ads")


def test_the_campaign_roster_names_campaigns_with_spend_by_their_public_labels(
    contracts: tuple[DataContract, ...],
) -> None:
    names = _Names({"campaign:a": "Zephyr", "campaign:b": "Aurora", "campaign:z": "Unused"})
    roster = _reader(contracts, names).roster(_CAMPAIGNS)
    assert roster.entries == (
        RosterEntry("campaign:b", "Aurora"),
        RosterEntry("campaign:a", "Zephyr"),
    )


def test_a_campaign_roster_without_public_names_is_refused_never_named_by_key(
    contracts: tuple[DataContract, ...],
) -> None:
    with pytest.raises(DomainError) as refused:
        _reader(contracts, None).roster(_CAMPAIGNS)
    assert (refused.value.code, refused.value.description) == (
        ROSTER_REFUSED,
        "Campaign names can't be read in this runtime, so no per-campaign breakdown is answered.",
    )
    with pytest.raises(DomainError) as unnamed:
        _reader(contracts, _Names({"campaign:a": "Zephyr"})).roster(_CAMPAIGNS)
    assert unnamed.value.description == (
        "1 campaign(s) with ad spend have no public name, so no per-campaign breakdown is answered."
    )
    # Control: an ad-product roster needs no campaign names.
    ad_products = replace(
        _reader(contracts, None),
        queries=_Spend(
            (
                {
                    "ad_product_key": "sponsored_products",
                    "currency_code": "USD",
                    measure_alias(0): Decimal("1.0000"),
                },
            )
        ),
    )
    assert ad_products.roster(replace(_CAMPAIGNS, kind=RosterKind.AD_PRODUCT)).keys == (
        "sponsored_products",
    )


def test_the_production_resolver_refuses_a_campaign_roster_with_an_unresolved_name(
    contracts: tuple[DataContract, ...],
) -> None:
    """Review B1-2: an answer's "Unnamed campaign 1" placeholder never names a roster entry."""
    unresolved = DimensionLabelResolver(
        _LabelCatalog({("campaign_key", "campaign:b"): "Aurora"}), SCOPE
    )
    with pytest.raises(DomainError) as refused:
        _reader(contracts, unresolved).roster(_CAMPAIGNS)
    assert (refused.value.code, refused.value.description) == (
        ROSTER_REFUSED,
        "1 campaign(s) with ad spend have no public name, so no per-campaign breakdown is "
        "answered.",
    )
    # The same resolver still gives an answer's rows the placeholder, as before.
    assert [
        entry.label
        for entry in unresolved.resolve(
            ({"campaign_key": "campaign:a"},), ("campaign_key",), SEPTEMBER
        ).entries
    ] == ["Unnamed campaign 1"]
    # Control: every campaign with spend resolved names the roster.
    resolved = DimensionLabelResolver(
        _LabelCatalog(
            {("campaign_key", "campaign:a"): "Zephyr", ("campaign_key", "campaign:b"): "Aurora"}
        ),
        SCOPE,
    )
    assert _reader(contracts, resolved).roster(_CAMPAIGNS).entries == (
        RosterEntry("campaign:b", "Aurora"),
        RosterEntry("campaign:a", "Zephyr"),
    )


def test_the_roster_reads_spend_under_the_requests_media_filter(
    contracts: tuple[DataContract, ...],
) -> None:
    reader = _reader(contracts, _Names({"campaign:a": "Zephyr", "campaign:b": "Aurora"}))
    filtered = replace(_CAMPAIGNS, filters=(("ad_product_key", "sponsored_products"),))
    roster = reader.roster(filtered)
    spend = cast("_Spend", reader.queries)
    assert spend.seen[0].plan.logical.filters["ad_product_key"] == "sponsored_products"
    assert roster.filters == (("ad_product_key", "sponsored_products"),)
    # Control: unfiltered, the spend read carries no media filter.
    reader.roster(_CAMPAIGNS)
    assert "ad_product_key" not in spend.seen[1].plan.logical.filters


# --- Fixtures: one delivery per calendar month, a withdrawn month and a graded one ---------------


def _fixture_rows() -> tuple[dict[str, str], ...]:
    return tuple(csv.DictReader(io.StringIO((FIXTURES / f"{PACK_ID}.csv").read_text())))


def _spend_campaigns(month: str) -> tuple[str, ...]:
    """Campaigns with spend in the fixture month, from both fct_ad_exposure spend writers."""
    sponsored = csv.DictReader(io.StringIO((FIXTURES / "amc-reach-frequency.csv").read_text()))
    dsp = csv.DictReader(io.StringIO((FIXTURES / "amc-dsp-impressions.csv").read_text()))
    return tuple(
        sorted(
            {
                *(
                    row["campaign_id_string"]
                    for row in sponsored
                    if row["event_date_utc"].startswith(month) and Decimal(row["spend"]) > 0
                ),
                *(
                    row["campaign_id_string"]
                    for row in dsp
                    if row["impression_date_utc"].startswith(month)
                    and Decimal(row["total_cost"]) > 0
                ),
            }
        )
    )


def _month_outcome(month: str) -> tuple[object, tuple[dict[str, str], ...]]:
    rows = tuple(row for row in _fixture_rows() if row["time_window_start"].startswith(month))
    keys = _spend_campaigns(month)
    first = date.fromisoformat(f"{month}-01")
    roster = Roster(
        RosterKind.CAMPAIGN,
        DateRange(first, (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)),
        US,
        "amazon_ads",
        tuple(RosterEntry(key, key) for key in keys),
    )
    kept = tuple(row["campaign_id_string"] for row in rows if row["rows_suppressed"] == "false")
    return apply_roster(roster, kept, coarser_grain_set(FACT, "campaign")), rows


def test_the_fixture_is_one_delivery_per_calendar_month() -> None:
    windows = {(row["time_window_start"], row["time_window_end"]) for row in _fixture_rows()}
    assert len(windows) == 15
    for start, end in windows:
        first = date.fromisoformat(start[:10])
        after = date.fromisoformat(end[:10])
        assert first.day == 1
        assert (after.day, after.month) == (1, first.month % 12 + 1)


def test_the_withdrawn_fixture_month_loses_more_than_a_fifth_of_a_20_plus_roster() -> None:
    outcome, rows = _month_outcome("2026-09")
    assert outcome.loss.expected >= 20  # type: ignore[attr-defined]
    assert outcome.loss.grade is LossGrade.WITHDRAWN  # type: ignore[attr-defined]
    assert outcome.off_roster == ()  # type: ignore[attr-defined]
    # One merged suppressed row, keys NULL, for the campaigns it dropped.
    assert [
        (row["campaign_id_string"], row["ad_product_type"])
        for row in rows
        if row["rows_suppressed"] == "true"
    ] == [("", "")]


def test_the_graded_fixture_month_drops_only_the_declared_campaign() -> None:
    outcome, rows = _month_outcome("2026-08")
    declared = next(
        item
        for item in load_synthetic_entity_catalog(ROOT).campaigns
        if item.key == "campaign:northstar-dsp-06"
    )
    alias = next(iter(declared.source_aliases.values()))
    assert outcome.loss.grade is not LossGrade.WITHDRAWN  # type: ignore[attr-defined]
    assert alias in {entry.key for entry in outcome.absent}  # type: ignore[attr-defined]
    assert alias not in {row["campaign_id_string"] for row in rows}
    # Control: the month before returns every campaign with a credited purchase, none dropped.
    july, july_rows = _month_outcome("2026-07")
    assert all(row["rows_suppressed"] == "false" for row in july_rows)
    assert alias in {row["campaign_id_string"] for row in july_rows}
    assert july.loss.grade is not LossGrade.WITHDRAWN  # type: ignore[attr-defined]
