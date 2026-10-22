# SPDX-License-Identifier: MIT-0
"""Pack 2: new buyers per ad product (`amc-new-to-brand-buyers-channel`) and the roster (plan D4).

The channel grain set of `fct_new_to_brand_buyers`: one row per ad product per complete ISO week
or calendar month, its own extraction (D-1), never added across ad products (D-2), read against
the ad products with spend in the same period (design §4.5). Every figure is fictional and
synthetic. The load tests run the real ingestion path on in-memory tooling; the chat tests run
the real planner and measurement tool.
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
from test_new_to_brand_buyers import HEADER as PLATFORM_HEADER
from test_new_to_brand_buyers import (
    _Current,  # pyright: ignore[reportPrivateUsage]
)
from test_new_to_brand_buyers import (
    _delivery as _platform_delivery,  # pyright: ignore[reportPrivateUsage]
)
from test_new_to_brand_buyers import (
    _load as _load_platform,  # pyright: ignore[reportPrivateUsage]
)
from test_semantic_sidecar import (
    MemoryCanonical,
    _case,  # pyright: ignore[reportPrivateUsage]
)

from adapters.campaign_measurement import (
    CampaignArtifactMeasurementService,
    CampaignMeasurementDependencies,
)
from adapters.inbound.tool_handlers.measurement import (
    MeasurementExecutionTooling,
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    bind_batch,
    execute_batch,
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
from adapters.jobs.ingest_delivery.semantic_sidecar import SemanticSidecarOutcome
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import SELECTION_FAILED, PublicationSelector
from adapters.outbound.source_pack import load_source_packs
from adapters.outbound.spend_roster import SpendRosterReader, ad_product_labels
from adapters.producer.packs import discard_policies, load_query_packs, pack_by_id
from domain.catalog.capability import GRAIN_NOT_SUPPORTED_CODE, Catalog
from domain.curation.landing import GOVERNED_MANIFEST_LAYOUT, LandedObject
from domain.data_contracts.contract import DataContract
from domain.measurement.planning import (
    GRAIN_COLUMNS,
    FactShape,
    FactShapes,
    PlanRequest,
    measure_alias,
    plan,
)
from domain.measurement.rosters import (
    ROSTER_REFUSED,
    ROSTER_UNAVAILABLE,
    Roster,
    RosterEntry,
    RosterKind,
    apply_roster,
    coarser_grain_set,
    roster_lines,
)
from domain.operations.bake_refusals import IsolatedRefusal, IsolatedRefusalReason, RefusalScope
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.publication import publication_lens_for
from domain.semantics.source_aligned import LandedDeliveryRef
from domain.semantics.time import TimeGrain
from domain.shared_kernel.errors import DomainError, InvariantError, ValidationError
from domain.shared_kernel.provenance import Suppression
from domain.shared_kernel.values import DateRange
from domain.shopper_aggregates.loss import DEGRADED_NOTE, LossGrade
from ports.outbound.canonical import CanonicalDestination
from ports.outbound.publication_selection import PublicationSelectionRequest
from ports.outbound.query import QueryRequest, QueryResult, RowCeiling, row_ceiling
from ports.outbound.rosters import RosterRequest
from ports.outbound.served_periods import ServedPeriods, ServedPeriodsRequest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-099", "ADR-100", "ADR-106")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/generated"
PACK_ID = "amc-new-to-brand-buyers-channel"
PLATFORM = "amc-new-to-brand-buyers-platform"
FACT = "fct_new_to_brand_buyers"
US = "ATVPDKIKX0DER"
NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)
WEEK = DateRange(date(2026, 9, 14), date(2026, 9, 20))
SEPTEMBER = DateRange(date(2026, 9, 1), date(2026, 9, 30))
SEPTEMBER_PERIOD = CalendarPeriod(SEPTEMBER.start, SEPTEMBER.end, TimeGrain.MONTH)
HEADER = (
    "conversion_event_marketplace_name,ad_product_type,buyers_new_to_brand_12mo,"
    "buyers_attributed,time_window_start,time_window_end,distinct_user_count,"
    "suppression_reason,rows_suppressed"
)
WINDOW = "2026-09-14T00:00:00.000Z,2026-09-21T00:00:00.000Z"
SP_ROW = f"AMAZON.COM,sponsored_products,2400,4800,{WINDOW},2,,false"
DSP_ROW = f"AMAZON.COM,amazon_dsp,1500,3100,{WINDOW},2,,false"
WITHHELD = f",,1,1,{WINDOW},1,1 amazon_attributed_events_by_conversion_time users < 2 required,true"
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


def test_the_pack_is_pack_1_with_the_ad_product_added_and_the_same_policies() -> None:
    channel, platform = pack_by_id(ROOT, PACK_ID), pack_by_id(ROOT, PLATFORM)
    coalesced = "COALESCE(ad_product_type, 'amazon_dsp')"
    assert channel.sql == (
        platform.sql.replace(
            "SELECT conversion_event_marketplace_name, ",
            f"SELECT conversion_event_marketplace_name, {coalesced} AS ad_product_type, ",
        ).replace(
            "GROUP BY conversion_event_marketplace_name",
            f"GROUP BY conversion_event_marketplace_name, {coalesced}",
        )
    )
    assert channel.expected_columns == (
        platform.expected_columns[0],
        "ad_product_type",
        *platform.expected_columns[1:],
    )
    assert (
        channel.period_policy,
        channel.expected_bar,
        channel.effective_cadence_lag_days,
        channel.input_parameters,
        channel.suppression_policy,
    ) == (
        platform.period_policy,
        platform.expected_bar,
        platform.effective_cadence_lag_days,
        platform.input_parameters,
        platform.suppression_policy,
    )
    assert any(item.contract_id == PACK_ID for item in discard_policies(load_query_packs(ROOT)))


def test_the_generated_tables_add_one_source_aligned_table_and_no_fact_column() -> None:
    generated = json.loads((ROOT / "contracts/generated/v2-tables.json").read_text())
    source = {table["name"]: table for table in generated["sourceAlignedTables"]}
    assert source["sa_amc_new_to_brand_buyers_channel"]["partitionKeys"] == [
        "source_publication_id"
    ]
    for layer in ("canonicalCandidateTables", "canonicalCurrentTables"):
        (fact,) = (table for table in generated[layer] if table["name"] == FACT)
        assert fact["writtenBy"] == ["amc-new-to-brand-buyers-campaign", PACK_ID, PLATFORM]
        # The 27 columns pack 1 created, unchanged in name, field id, type and nullability
        # (DataV2 append-only): a retype or a requiredness change fails here.
        assert [
            (column["name"], column["fieldId"], column["type"], column["nullable"])
            for column in fact["columns"]
        ] == [(name, index, *kind) for index, (name, *kind) in enumerate(FACT_COLUMNS, start=1)]
        # The grain gains the nullable ad product (and, with pack 3, the campaign), so the fact
        # declares no Iceberg identifier fields from here (live acceptance row 0 gates the deploy).
        assert fact["grain"] == [
            "publication_id",
            "ad_product_key",
            "campaign_key",
            "grain_set",
            "marketplace_key",
            "period_end",
            "period_start",
            "time_grain",
        ]


FACT_COLUMNS = (
    ("publication_id", "string", False),
    ("attempt_id", "string", False),
    ("partition_key", "string", False),
    ("publisher", "string", False),
    ("fencing_token", "bigint", False),
    ("source_publications_json", "string", False),
    ("source_revision", "string", False),
    ("content_checksum", "string", False),
    ("rule_set_version", "string", False),
    ("created_at", "timestamp", False),
    ("ad_product_key", "string", True),
    ("basis_key", "string", False),
    ("campaign_key", "string", True),
    ("channel_group_key", "string", True),
    ("channel_key", "string", True),
    ("ecosystem_key", "string", False),
    ("marketplace_key", "string", False),
    ("platform_key", "string", False),
    ("buyers_attributed", "bigint", True),
    ("buyers_new_to_brand_12mo", "bigint", True),
    ("cell_state", "string", False),
    ("grain_set", "string", False),
    ("identity_unit", "string", False),
    ("period_end", "date", False),
    ("period_start", "date", False),
    ("source_timezone", "string", False),
    ("time_grain", "string", False),
)
"""`fct_new_to_brand_buyers` as pack 1 created it: (name, type, nullable), field ids from 1."""


# --- Load: discard, no closed enumeration, media from the ad product ------------------------------


def _delivery(text: str, window: DateRange = WEEK) -> LoadedDelivery:
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
    publications: tuple[str, ...] = ()
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
            operation_id="ntb-channel-unit",
            execution_id="ntb-channel-execution",
            plan_digest="sha256:ntb-channel-plan",
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
        publications=tuple(item.value for result in results for item in result.publications),
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


_CHANNEL_ROW = {
    "basis_key": "ad_attributed",
    "campaign_key": None,
    "ecosystem_key": "amazon",
    "grain_set": "channel",
    "identity_unit": "amazon_user_id",
    "marketplace_key": "amazon_us",
    "platform_key": "amazon_ads",
    "source_timezone": "UTC",
    "cell_state": "returned",
    "period_end": WEEK.end,
    "period_start": WEEK.start,
    "time_grain": "week",
}


def test_returned_ad_products_publish_with_taxonomy_media_and_a_withheld_one_is_dropped() -> None:
    loaded = _load(_delivery(_csv(DSP_ROW, SP_ROW, WITHHELD)))
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED
    assert sorted(loaded.rows, key=lambda row: str(row["ad_product_key"])) == [
        {
            **_CHANNEL_ROW,
            "ad_product_key": "amazon_dsp",
            "channel_key": "amazon_dsp",
            "channel_group_key": "display",
            "buyers_attributed": 3100,
            "buyers_new_to_brand_12mo": 1500,
        },
        {
            **_CHANNEL_ROW,
            "ad_product_key": "sponsored_products",
            "channel_key": "amazon_sponsored_products",
            "channel_group_key": "search",
            "buyers_attributed": 4800,
            "buyers_new_to_brand_12mo": 2400,
        },
    ]
    # No closed enumeration at load: each returned ad product is its own cell, none is missing
    # here, and the withheld row is only counted. The roster grades the loss at serve.
    assert loaded.loss == (
        '{"discarded_rows":1,"expected_cells":2,"grade":"not_degraded",'
        '"loss_ratio":"0","not_returned_cells":0}'
    )


def test_an_empty_channel_delivery_writes_no_cell_while_pack_1_writes_its_not_returned_cell() -> (
    None
):
    """The channel grain set has no closed enumeration; the roster supplies "not available"."""
    loaded = _load(_delivery(_csv(WITHHELD)))
    assert (loaded.outcome, loaded.rows) == (SemanticSidecarOutcome.PUBLISHED, ())
    assert loaded.loss == (
        '{"discarded_rows":1,"expected_cells":0,"grade":"not_degraded",'
        '"loss_ratio":"0","not_returned_cells":0}'
    )
    # Control: the platform grain set's empty delivery writes its one marketplace cell.
    platform = _load_platform(_platform_delivery(f"{PLATFORM_HEADER}\n"))
    assert [row["cell_state"] for row in platform.rows] == ["not_returned"]


def test_a_repeated_ad_product_rejects_the_delivery() -> None:
    blocked = _load(_delivery(_csv(SP_ROW, SP_ROW)))
    assert (blocked.outcome, blocked.publications) == (SemanticSidecarOutcome.BLOCKED, ())
    assert blocked.contract_rule == (
        "fail",
        f"{PACK_ID}: CELL_DUPLICATE: row 1 repeats cell ad_product_key=sponsored_products, "
        "marketplace_key=amazon_us.",
    )
    # Positive control: two different ad products publish.
    assert _load(_delivery(_csv(SP_ROW, DSP_ROW))).outcome is SemanticSidecarOutcome.PUBLISHED


# --- Planner: D-1 grain sets, D-2 no sum across ad products, D-6 spend days -----------------------


def _roster(window: DateRange, *keys: str) -> Roster:
    labels = ad_product_labels(load_media_taxonomy(ROOT))
    return Roster(
        RosterKind.AD_PRODUCT,
        window,
        US,
        "amazon_ads",
        tuple(RosterEntry(key, labels[key]) for key in keys),
    )


def _filters(grain_set: str, grain: str = "month") -> dict[str, str]:
    return {"time_grain": grain, "grain_set": grain_set, "marketplace_key": US}


def _refused(call: object) -> DomainError:
    with pytest.raises(DomainError) as refused:
        call()  # type: ignore[operator]
    return refused.value


def test_an_ad_product_request_plans_at_grain_set_channel_and_a_platform_request_at_platform(
    catalog: Catalog, contracts: tuple[DataContract, ...]
) -> None:
    shape = FactShape.for_fact(FACT, contracts)
    roster = _roster(SEPTEMBER, "sponsored_products")
    channel = plan(
        catalog,
        PlanRequest(
            "buyers_new_to_brand_12mo",
            ("ad_product",),
            SEPTEMBER,
            _filters("channel"),
            roster=roster,
        ),
        shape,
    )
    assert (channel.group_by, channel.roster) == (("ad_product_key",), roster)
    platform = plan(
        catalog,
        PlanRequest("buyers_new_to_brand_12mo", ("marketplace",), SEPTEMBER, _filters("platform")),
        shape,
    )
    assert (platform.group_by, platform.roster) == (("marketplace_key",), None)
    # D-1: the platform grain set stores the ad product NULL, so it never answers per ad product.
    error = _refused(
        lambda: plan(
            catalog,
            PlanRequest(
                "buyers_new_to_brand_12mo", ("ad_product",), SEPTEMBER, _filters("platform")
            ),
            shape,
        )
    )
    assert error.code == GRAIN_NOT_SUPPORTED_CODE
    assert "at grain set platform doesn't split by ad_product_key" in error.description


def test_a_sum_across_ad_products_is_refused_beside_the_per_ad_product_figure(
    catalog: Catalog, contracts: tuple[DataContract, ...]
) -> None:
    shape = FactShape.for_fact(FACT, contracts)
    roster = _roster(SEPTEMBER, "sponsored_products")
    error = _refused(
        lambda: plan(
            catalog,
            PlanRequest(
                "buyers_new_to_brand_12mo",
                ("marketplace",),
                SEPTEMBER,
                _filters("channel"),
                roster=roster,
            ),
            shape,
        )
    )
    assert error.code == GRAIN_NOT_SUPPORTED_CODE
    assert "would sum across ad_product_key" in error.description
    # An unpinned grain set reads neither: it would sum across both grain sets.
    unpinned = {"time_grain": "month", "marketplace_key": US}
    error = _refused(
        lambda: plan(
            catalog,
            PlanRequest("buyers_attributed", ("ad_product",), SEPTEMBER, unpinned),
            shape,
        )
    )
    assert (error.code, error.context) == (
        GRAIN_NOT_SUPPORTED_CODE,
        {"requested": "buyers_attributed", "reason": "grain_not_supported"},
    )
    assert error.description.startswith(
        "buyers_attributed over fct_new_to_brand_buyers would sum across grain_set, "
    )
    # Positive control: the same question per ad product plans.
    assert plan(
        catalog,
        PlanRequest(
            "buyers_attributed", ("ad_product",), SEPTEMBER, _filters("channel"), roster=roster
        ),
        shape,
    ).group_by == ("ad_product_key",)


def test_a_channel_alone_is_refused_at_either_grain_set_beside_channel_and_ad_product(
    catalog: Catalog, contracts: tuple[DataContract, ...]
) -> None:
    """A channel holds several ad products, so per channel alone reads no grain set (D-1, D-2)."""
    shape = FactShape.for_fact(FACT, contracts)
    roster = _roster(SEPTEMBER, "sponsored_products")
    # Chat names only the channel, so it picks grain set platform, which stores channel empty.
    at_platform = _refused(
        lambda: plan(
            catalog,
            PlanRequest("buyers_new_to_brand_12mo", ("channel",), SEPTEMBER, _filters("platform")),
            shape,
        )
    )
    assert (at_platform.code, at_platform.description) == (
        GRAIN_NOT_SUPPORTED_CODE,
        "buyers_new_to_brand_12mo at grain set platform doesn't split by channel_key: that "
        "grain set stores it empty. Each grain set is its own extraction, so a figure is read "
        "only at the grain set that measured it, never derived from another. Available "
        "instead: marketplace, ad_product, campaign.",
    )
    # Each offered grain plans alone; channel, the refused one, isn't offered back. Campaign
    # (pack 3) plans alone in test_new_to_brand_buyers_campaign.py.
    for offered, grain_set in (("marketplace", "platform"), ("ad_product", "channel")):
        assert plan(
            catalog,
            PlanRequest(
                "buyers_new_to_brand_12mo",
                (offered,),
                SEPTEMBER,
                _filters(grain_set),
                roster=roster if grain_set == "channel" else None,
            ),
            shape,
        ).group_by == (GRAIN_COLUMNS[offered],)
    chat = _refused(
        lambda: _chat(contracts, ("channel",), _ad_rows("sponsored_products"), _Rosters(ROSTER_5))
    )
    assert (chat.code, chat.description) == (at_platform.code, at_platform.description)
    # Pinned to grain set channel it would add the channel's ad products together.
    at_channel = _refused(
        lambda: plan(
            catalog,
            PlanRequest(
                "buyers_new_to_brand_12mo",
                ("channel",),
                SEPTEMBER,
                _filters("channel"),
                roster=roster,
            ),
            shape,
        )
    )
    assert at_channel.code == GRAIN_NOT_SUPPORTED_CODE
    assert at_channel.description.startswith(
        "buyers_new_to_brand_12mo over fct_new_to_brand_buyers would sum across ad_product_key, "
    )
    # Positive control: channel and ad product together plan at grain set channel, as the bake.
    both = plan(
        catalog,
        PlanRequest(
            "buyers_new_to_brand_12mo",
            ("channel", "ad_product"),
            SEPTEMBER,
            _filters("channel"),
            roster=roster,
        ),
        shape,
    )
    assert (both.group_by, both.roster) == (("channel_key", "ad_product_key"), roster)


def test_a_channel_grain_set_figure_without_its_roster_is_refused(
    catalog: Catalog, contracts: tuple[DataContract, ...]
) -> None:
    shape = FactShape.for_fact(FACT, contracts)
    request = PlanRequest(
        "buyers_new_to_brand_12mo", ("ad_product",), SEPTEMBER, _filters("channel")
    )
    with pytest.raises(InvariantError) as refused:
        plan(catalog, request, shape)
    assert refused.value.code == ROSTER_UNAVAILABLE
    assert refused.value.description == (
        "A figure graded against a roster was planned without the roster for its period."
    )
    # Another period's roster is refused the same way; the period's own plans.
    with pytest.raises(InvariantError) as other_period:
        plan(catalog, replace(request, roster=_roster(WEEK, "sponsored_products")), shape)
    assert (other_period.value.code, other_period.value.context) == (
        ROSTER_UNAVAILABLE,
        {"roster": "ad_product", "window": "2026-09-01..2026-09-30"},
    )
    assert plan(catalog, replace(request, roster=_roster(SEPTEMBER)), shape).roster is not None


def _cost(contracts: tuple[DataContract, ...], catalog: Catalog) -> object:
    return plan(
        catalog,
        PlanRequest(
            "cost_per_new_to_brand_buyer_12mo",
            ("ad_product",),
            SEPTEMBER,
            _filters("channel"),
            roster=_roster(SEPTEMBER, "sponsored_products"),
        ),
        FactShapes(
            numerator=FactShape.for_fact("fct_ad_exposure", contracts),
            denominator=FactShape.for_fact(FACT, contracts),
        ),
    )


def test_d6_cost_per_new_buyer_per_ad_product_needs_every_spend_day_of_the_month(
    catalog: Catalog, contracts: tuple[DataContract, ...]
) -> None:
    logical = _cost(contracts, catalog)
    assert logical.group_by == ("ad_product_key",)  # type: ignore[attr-defined]
    selector = PublicationSelector(
        _Current(), load_source_packs(ROOT), load_commerce_identity_bridge(ROOT)
    )
    bound = selector.select(PublicationSelectionRequest((logical,), contracts, NOW))  # type: ignore[arg-type]
    spend_days = {
        item.partition.bucket.window.start
        for item in bound.plans[0].primary
        if item.partition.scope.dataset.value == "fct_ad_exposure"
    }
    assert spend_days == {SEPTEMBER.start + timedelta(days=offset) for offset in range(30)}
    # Only the channel grain set's publication for the month is bound, never pack 1's.
    pack = next(item for item in load_source_packs(ROOT) if item.contract_id == PACK_ID)
    lens = publication_lens_for(
        "source_pack.amc_new_to_brand_buyers_channel",
        (("period_grain", '"month"'),),
        pack.registry_version,
    )
    assert [item.partition.scope.lens for item in bound.plans[0].joined] == [lens]
    # Control: the platform figure binds pack 1's publication alone, never pack 2's.
    platform = plan(
        catalog,
        PlanRequest(
            "cost_per_new_to_brand_buyer_12mo", ("marketplace",), SEPTEMBER, _filters("platform")
        ),
        FactShapes(
            numerator=FactShape.for_fact("fct_ad_exposure", contracts),
            denominator=FactShape.for_fact(FACT, contracts),
        ),
    )
    platform_bound = selector.select(PublicationSelectionRequest((platform,), contracts, NOW))
    platform_pack = next(item for item in load_source_packs(ROOT) if item.contract_id == PLATFORM)
    assert [item.partition.scope.lens for item in platform_bound.plans[0].joined] == [
        publication_lens_for(
            "source_pack.amc_new_to_brand_buyers_platform",
            (("period_grain", '"month"'),),
            platform_pack.registry_version,
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
        gap.select(PublicationSelectionRequest((logical,), contracts, NOW))  # type: ignore[arg-type]
    assert refused.value.code == SELECTION_FAILED
    assert refused.value.description.startswith("No published data covers the requested window")


# --- Chat: the grain set chosen, and the roster in the answer -------------------------------------


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


@dataclass
class _Served:
    def served_periods(self, request: ServedPeriodsRequest) -> ServedPeriods:
        published = {contract.contract_id for contract in request.contracts}
        return ServedPeriods(
            frozenset((contract_id, SEPTEMBER_PERIOD) for contract_id in published),
            dict.fromkeys(published, 2),
        )


@dataclass
class _Rosters:
    keys: tuple[str, ...]
    seen: list[RosterRequest] = field(default_factory=list[RosterRequest])

    def roster(self, request: RosterRequest) -> Roster:
        self.seen.append(request)
        return _roster(request.period, *self.keys)


def _chat(  # noqa: PLR0913, PLR0917 — the question, what comes back, and the roster reader
    contracts: tuple[DataContract, ...],
    grains: tuple[str, ...],
    rows: tuple[dict[str, object], ...],
    rosters: _Rosters | None,
    filters: dict[str, str] | None = None,
) -> tuple[object, _Rows]:
    queries = _Rows(rows)
    response = handle(
        MeasurementToolRequest(
            tool=MeasurementTool.METRIC_BY_GRAIN,
            catalog=load_catalog(ROOT),
            contracts=contracts,
            metric_name="buyers_new_to_brand_12mo",
            grains=grains,
            window=SEPTEMBER,
            computed_at=NOW,
            correlation_id="corr-ntb-channel",
            filters={"marketplace_key": US} if filters is None else filters,
        ),
        MeasurementTooling(
            queries=queries,
            publications=StubPublications(),
            periods=_Served(),
            rosters=rosters,
        ),
    )
    return response, queries


ROSTER_5 = (
    "amazon_dsp",
    "sponsored_products",
    "sponsored_brands",
    "sponsored_display",
    "sponsored_television",
)


def _ad_rows(*keys: str) -> tuple[dict[str, object], ...]:
    return tuple(
        {"ad_product_key": key, measure_alias(0): 100 + index} for index, key in enumerate(keys)
    )


def test_a_5_entry_roster_missing_one_names_it_not_available_graded_never_withdrawn(
    contracts: tuple[DataContract, ...],
) -> None:
    """Design §4.4 case 4: a 5-entry roster missing one (20% loss) is graded, never withdrawn."""
    rosters = _Rosters(ROSTER_5)
    response, queries = _chat(contracts, ("ad_product",), _ad_rows(*ROSTER_5[:4]), rosters)
    planned = queries.seen[0].plan.logical
    assert dict(planned.filters) == {
        "grain_set": "channel",
        "marketplace_key": US,
        "time_grain": "month",
    }
    assert [(item.kind, item.period, item.marketplace_key) for item in rosters.seen] == [
        (RosterKind.AD_PRODUCT, SEPTEMBER, US)
    ]
    narrative = cast("tuple[str, ...]", response.narrative)  # type: ignore[attr-defined]
    assert "Sponsored Television is not available: too few shoppers, or none." in narrative
    assert DEGRADED_NOTE in narrative
    assert len(response.data.values) == 4  # type: ignore[attr-defined]
    outcome = apply_roster(
        _roster(SEPTEMBER, *ROSTER_5), ROSTER_5[:4], coarser_grain_set(FACT, "channel")
    )
    assert (outcome.loss.grade, outcome.loss.ratio, outcome.loss.pointer) == (
        LossGrade.DEGRADED,
        Decimal("0.2"),
        None,
    )
    # Control: every rostered ad product returned names none and isn't degraded.
    whole, _ = _chat(contracts, ("ad_product",), _ad_rows(*ROSTER_5), _Rosters(ROSTER_5))
    lines = cast("tuple[str, ...]", whole.narrative)  # type: ignore[attr-defined]
    assert not any("is not available" in line for line in lines)
    assert DEGRADED_NOTE not in lines


def test_a_20_entry_roster_missing_more_than_a_fifth_is_withdrawn_entry_by_entry() -> None:
    """The withdrawal branch: every rostered entry reads not available, with the pointer."""
    roster = Roster(
        RosterKind.CAMPAIGN,
        SEPTEMBER,
        US,
        "amazon_ads",
        tuple(RosterEntry(f"campaign-{index:02d}", f"Campaign {index:02d}") for index in range(20)),
    )
    coarser = coarser_grain_set(FACT, "channel")
    withdrawn = apply_roster(roster, roster.keys[:15], coarser)
    assert (withdrawn.loss.grade, len(withdrawn.absent)) == (LossGrade.WITHDRAWN, 5)
    assert roster_lines(withdrawn) == tuple(
        f"Campaign {index:02d} is not available: too few shoppers in this breakdown; see new "
        "buyers for the whole Amazon Ads network."
        for index in range(20)
    )
    # Control: a fifth missing is only degraded, and each missing entry is named.
    degraded = apply_roster(roster, roster.keys[:16], coarser)
    assert degraded.loss.grade is LossGrade.DEGRADED
    assert roster_lines(degraded) == (
        *(
            f"Campaign {index:02d} is not available: too few shoppers, or none."
            for index in (16, 17, 18, 19)
        ),
        DEGRADED_NOTE,
    )
    # Control: under 20 entries the same share missing never withdraws.
    short = replace(roster, entries=roster.entries[:19])
    assert apply_roster(short, short.keys[:14], coarser).loss.grade is LossGrade.DEGRADED


@dataclass
class _WideRoster:
    """A 20-entry ad-product roster: the five governed ad products and fifteen more."""

    def roster(self, request: RosterRequest) -> Roster:
        labels = ad_product_labels(load_media_taxonomy(ROOT))
        return Roster(
            RosterKind.AD_PRODUCT,
            request.period,
            US,
            "amazon_ads",
            (
                *(RosterEntry(key, labels[key]) for key in ROSTER_5),
                *(
                    RosterEntry(f"ad-product-{index:02d}", f"Ad product {index:02d}")
                    for index in range(15)
                ),
            ),
        )


def test_a_withdrawn_roster_in_chat_offers_none_of_its_figures(
    contracts: tuple[DataContract, ...],
) -> None:
    """Design §4.4 (FEAT-004-A2-4, built in FEAT-005): a withdrawn grain set shows no figure.

    Every rostered entry reads "not available" with the pointer instead.
    """
    response, _ = _chat(contracts, ("ad_product",), _ad_rows(*ROSTER_5), _WideRoster())  # type: ignore[arg-type]
    narrative = cast("tuple[str, ...]", response.narrative)  # type: ignore[attr-defined]
    pointer = "too few shoppers in this breakdown; see new buyers for the whole Amazon Ads network"
    assert narrative[0] == (
        "New-to-brand ad-attributed buyers (12 month lookback) · 14-day attribution · "
        "conversion date · advertised products for 30 day(s), 2026-09-01 to 2026-09-30, "
        f"broken down by ad product, isn't offered: {pointer}."
    )
    assert f"Sponsored Products is not available: {pointer}." in narrative
    assert f"Ad product 14 is not available: {pointer}." in narrative
    assert response.data.values == ()  # type: ignore[attr-defined]


def test_a_returned_ad_product_off_the_roster_is_kept(contracts: tuple[DataContract, ...]) -> None:
    response, _ = _chat(
        contracts,
        ("ad_product",),
        _ad_rows("sponsored_products", "sponsored_brands"),
        _Rosters(("sponsored_products",)),
    )
    assert [row["ad_product_key"] for row in response.data.values] == [  # type: ignore[attr-defined]
        "sponsored_products",
        "sponsored_brands",
    ]
    assert not any(
        "is not available" in line
        for line in cast("tuple[str, ...]", response.narrative)  # type: ignore[attr-defined]
    )


def test_a_marketplace_request_still_reads_pack_1_and_reads_no_roster(
    contracts: tuple[DataContract, ...],
) -> None:
    rosters = _Rosters(ROSTER_5)
    _, queries = _chat(
        contracts,
        ("marketplace",),
        ({"marketplace_key": "amazon_us", measure_alias(0): 9},),
        rosters,
    )
    assert queries.seen[0].plan.logical.filters["grain_set"] == "platform"
    assert rosters.seen == []


def test_a_per_ad_product_question_needs_its_roster_reader_and_one_marketplace(
    contracts: tuple[DataContract, ...],
) -> None:
    with pytest.raises(InvariantError) as unavailable:
        _chat(contracts, ("ad_product",), _ad_rows("sponsored_products"), None)
    assert unavailable.value.code == ROSTER_UNAVAILABLE
    with pytest.raises(ValidationError) as refused:
        _chat(
            contracts,
            ("ad_product",),
            _ad_rows("sponsored_products"),
            _Rosters(ROSTER_5),
            filters={"grain_set": "channel"},
        )
    assert refused.value.code == ROSTER_REFUSED
    # Positive control: with both, it answers.
    response, _ = _chat(
        contracts,
        ("ad_product",),
        _ad_rows("sponsored_products"),
        _Rosters(("sponsored_products",)),
    )
    assert len(response.data.values) == 1  # type: ignore[attr-defined]


def test_the_campaign_measurement_service_insights_use_reads_the_roster_like_chat(
    contracts: tuple[DataContract, ...], catalog: Catalog
) -> None:
    """Insight precompute and Campaign rendering prepare every request through `attach_roster`."""
    request = MeasurementToolRequest(
        tool=MeasurementTool.METRIC_BY_GRAIN,
        catalog=catalog,
        contracts=contracts,
        metric_name="buyers_new_to_brand_12mo",
        grains=("ad_product",),
        window=SEPTEMBER,
        computed_at=NOW,
        correlation_id="corr-ntb-insight",
        filters=_filters("channel"),
    )

    def answered(rows: tuple[dict[str, object], ...], rosters: _Rosters | None) -> object:
        service = CampaignArtifactMeasurementService(
            CampaignMeasurementDependencies(
                AuthorizedAnalyticalScope(
                    frozenset({load_commerce_identity_bridge(ROOT).commerce_scope})
                ),
                catalog,
                contracts,
                lambda: NOW,
                rosters=rosters,
            )
        )
        prepared = service._prepare((request,), "Insight")  # pyright: ignore[reportPrivateUsage]
        assert prepared.plans[0].logical.roster == _roster(SEPTEMBER, *ROSTER_5)
        (response,) = execute_batch(
            bind_batch(prepared, StubPublications()),
            MeasurementExecutionTooling(queries=_Rows(rows)),
        )
        return response

    missing = answered(_ad_rows(*ROSTER_5[:4]), _Rosters(ROSTER_5))
    narrative = cast("tuple[str, ...]", missing.narrative)  # type: ignore[attr-defined]
    assert "Sponsored Television is not available: too few shoppers, or none." in narrative
    assert DEGRADED_NOTE in narrative
    # Control: every rostered ad product returned names none and isn't degraded.
    whole = answered(_ad_rows(*ROSTER_5), _Rosters(ROSTER_5))
    lines = cast("tuple[str, ...]", whole.narrative)  # type: ignore[attr-defined]
    assert not any("is not available" in line for line in lines)
    assert DEGRADED_NOTE not in lines
    # Without the reader the figure is refused typed, never served short.
    with pytest.raises(InvariantError) as unavailable:
        answered(_ad_rows(*ROSTER_5), None)
    assert unavailable.value.code == ROSTER_UNAVAILABLE


# --- The roster reader: spend over every day of the period ----------------------------------------


@dataclass
class _Spend:
    rows: tuple[dict[str, object], ...]
    truncated: bool = False

    def run(self, request: QueryRequest) -> QueryResult:
        del request
        return QueryResult(
            rows=self.rows,  # type: ignore[arg-type]
            bytes_scanned=1,
            truncated=self.truncated,
            suppression=Suppression(rows_suppressed=False),
        )


def _reader(
    contracts: tuple[DataContract, ...], current: _Current, rows: tuple[dict[str, object], ...]
) -> SpendRosterReader:
    return SpendRosterReader(
        catalog=load_catalog(ROOT),
        contracts=contracts,
        publications=PublicationSelector(
            current, load_source_packs(ROOT), load_commerce_identity_bridge(ROOT)
        ),
        queries=_Spend(rows),
        labels=ad_product_labels(load_media_taxonomy(ROOT)),
        clock=lambda: NOW,
    )


_SPEND: tuple[dict[str, object], ...] = (
    {
        "ad_product_key": "sponsored_products",
        "currency_code": "USD",
        measure_alias(0): Decimal("10.0000"),
    },
    {"ad_product_key": "amazon_dsp", "currency_code": "USD", measure_alias(0): Decimal("4.0000")},
    {
        "ad_product_key": "sponsored_brands",
        "currency_code": "USD",
        measure_alias(0): Decimal("0.0000"),
    },
)
_REQUEST = RosterRequest(RosterKind.AD_PRODUCT, WEEK, US, "amazon_ads")


def test_the_roster_lists_ad_products_with_spend_in_taxonomy_order(
    contracts: tuple[DataContract, ...],
) -> None:
    roster = _reader(contracts, _Current(), _SPEND).roster(_REQUEST)
    assert roster == Roster(
        RosterKind.AD_PRODUCT,
        WEEK,
        US,
        "amazon_ads",
        (
            RosterEntry("amazon_dsp", "Amazon DSP"),
            RosterEntry("sponsored_products", "Sponsored Products"),
        ),
    )


def test_a_period_with_a_day_lacking_a_spend_publication_gives_no_roster(
    contracts: tuple[DataContract, ...],
) -> None:
    reader = _reader(contracts, _Current(), _SPEND)
    logical = plan(
        load_catalog(ROOT),
        PlanRequest(
            "ad_spend", ("ad_product",), WEEK, {"marketplace_key": US, "platform_key": "amazon_ads"}
        ),
        FactShape.for_fact("fct_ad_exposure", contracts),
    )
    bound = reader.publications.select(PublicationSelectionRequest((logical,), contracts, NOW))
    missing = next(
        item.partition.identity
        for item in bound.plans[0].primary
        if item.partition.bucket.window.start == date(2026, 9, 16)
    )
    with pytest.raises(DomainError) as refused:
        _reader(contracts, _Current(absent=frozenset({missing})), _SPEND).roster(_REQUEST)
    assert refused.value.code == ROSTER_REFUSED
    assert refused.value.description.startswith(
        "Which ad products had ad spend from 2026-09-14 to 2026-09-20 can't be read: "
    )


# --- Fixtures -------------------------------------------------------------------------------------


def test_the_fixture_has_one_rostered_ad_product_with_no_row_in_its_declared_week() -> None:
    rows = tuple(csv.DictReader(io.StringIO((FIXTURES / f"{PACK_ID}.csv").read_text())))
    week = [row for row in rows if row["time_window_start"] == "2026-09-28T00:00:00.000Z"]
    assert [(row["ad_product_type"], row["rows_suppressed"]) for row in week] == [
        ("amazon_dsp", "false"),
        ("", "true"),
        ("sponsored_products", "false"),
    ]
    # Sponsored Brands is on the roster that week: it had spend in the fct_ad_exposure fixture.
    spend = sum(
        Decimal(row["spend"])
        for row in csv.DictReader(io.StringIO((FIXTURES / "amc-reach-frequency.csv").read_text()))
        if row["ad_product_type"] == "sponsored_brands"
        and "2026-09-28" <= row["event_date_utc"] <= "2026-10-04"
    )
    assert spend > 0
    # Control: the week before returns every ad product with a credited purchase.
    before = {
        row["ad_product_type"]
        for row in rows
        if row["time_window_start"] == "2026-09-21T00:00:00.000Z"
    }
    assert before == {"amazon_dsp", "sponsored_brands", "sponsored_products"}


def test_a_truncated_bake_spend_roster_refuses_its_page_and_chat_keeps_its_answer(
    contracts: tuple[DataContract, ...],
) -> None:
    """ADR-142: at the bake ceiling the cut read is rows_over_ceiling with its width."""
    reader = replace(_reader(contracts, _Current(), _SPEND), queries=_Spend(_SPEND, truncated=True))
    with pytest.raises(IsolatedRefusal) as refused:
        reader.roster(replace(_REQUEST, ceiling=RowCeiling.BAKE))
    assert (refused.value.reason, refused.value.scope) == (
        IsolatedRefusalReason.ROWS_OVER_CEILING,
        RefusalScope.PAGE,
    )
    assert dict(refused.value.refusal_context) == {"max_rows": row_ceiling(RowCeiling.BAKE)}
    # Paired: chat's read keeps its own ROSTER_REFUSED answer.
    with pytest.raises(DomainError) as chat:
        reader.roster(_REQUEST)
    assert chat.value.code == ROSTER_REFUSED and not isinstance(chat.value, IsolatedRefusal)
