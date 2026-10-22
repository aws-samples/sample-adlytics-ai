# SPDX-License-Identifier: MIT-0
"""Serving New customers: the v2 route over its baked bodies (UI slice 9 New customers, B4, B6).

Every body here is baked by the real materializer (`RetainedNewCustomersPresetArtifactSource`
over the source tests' fictional query port) and served by the real handler, so the browser
fixtures this module writes are the server's own responses (plan B.2).
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from unittest.mock import patch

import pytest
import test_new_customers_source as nc
from test_presentation_handler import (
    METRIC_CATALOG,
    SCHEMAS,
    SESSION,
    RecordingCatalog,
    RecordingPages,
    _body,  # pyright: ignore[reportPrivateUsage]
    _event,  # pyright: ignore[reportPrivateUsage]
    _PresentationModule,  # pyright: ignore[reportPrivateUsage]
    presentation_module,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from adapters.jobs.preset_artifact_publisher.new_customers_source import (
    RetainedNewCustomersPresetArtifactSource,
    new_customers_coverage_requirements,
)
from adapters.jobs.preset_artifact_publisher.retail_source import (
    RetailProductHierarchyResult,
    RetailProductIdentity,
)
from domain.measurement.planning import measure_alias
from domain.presentation.absence_copy import AbsenceTrigger, absence_message
from domain.presentation.insights import InsightSlotId
from domain.presentation.new_customers import NewCustomersOutcome, resolve_new_customers_payload
from domain.presentation.preset_artifacts import (
    PresetArtifactBody,
    PresetArtifactPointer,
    PresetArtifactPointerRosterRequest,
    PresetArtifactPointerRosterResult,
    PresetArtifactPublicationRequest,
    PresetArtifactVariant,
)
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.identity import CommerceScopeKey
from domain.semantics.time import TimeGrain
from domain.shared_kernel.errors import ValidationError
from domain.shared_kernel.values import DateRange
from ports.outbound.query import QueryRequest, QueryRow

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-100", "ADR-107", "ADR-119", "ADR-120")]

PATH = "/api/presentation/new-customers"
PAGE = "new-customers"
_TOKENS = tuple(f"{letter * 43}" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "frontend/test/fixtures"
NOW = nc.NOW
"""23 Sep 2026: the latest complete week is 14-20 Sep and the latest complete month August."""
COVERED_FROM = date(2026, 7, 1)
WEEK = nc.WEEK
MONTH = CalendarPeriod(date(2026, 8, 1), date(2026, 8, 31), TimeGrain.MONTH)
THIRTY = DateRange(date(2026, 8, 22), date(2026, 9, 20))
WINDOWS = {
    "7d": nc.PERIOD,
    "30d": THIRTY,
    "last_complete_week": WEEK.window,
    "last_complete_month": MONTH.window,
}
SD: tuple[str, str] = ("amazon_sponsored_display", "sponsored_display")
SEVEN_DAY_SELECTION = {"temporal_selection": {"kind": "preset", "preset_id": "7d"}}
WEEK_SELECTION = {"temporal_selection": {"kind": "preset", "preset_id": "last_complete_week"}}
MONTH_SELECTION = {"temporal_selection": {"kind": "preset", "preset_id": "last_complete_month"}}


@dataclass
class _Queries(nc.NewCustomersQueries):
    """The source tests' fictional port, with every delivering ad product converting.

    The base port returns conversion rows for Sponsored Products only; the mockup state has
    conversions on all four ad products, so this one returns a row for each ad product that
    `_CONVERSION` names. A marketplace in `doubled` reads twice every step-1 figure, so two
    marketplaces' tables differ and a table under the wrong marketplace would show.
    """

    doubled: frozenset[str] = frozenset()

    def _scaled(self, value: Decimal | None, market: str) -> Decimal | None:
        return value if value is None or market not in self.doubled else value * 2

    def _media_rows(self, request: QueryRequest, market: str) -> list[QueryRow]:
        plan = request.plan.logical
        current = plan.window == self.period
        delivered = plan.join is not None or plan.metric.name == "ad_spend"
        conversion = nc._CONVERSION  # pyright: ignore[reportPrivateUsage]
        converting = {media for table in conversion.values() for media in table}
        roster = tuple(
            media
            for media in nc._DELIVERY["ad_spend"]  # pyright: ignore[reportPrivateUsage]
            if delivered or media in converting
        )
        rows: list[QueryRow] = []
        for media in roster:
            row: dict[str, str | int | Decimal | None] = {
                "ecosystem_key": "amazon",
                "platform_key": "amazon_ads",
                "channel_key": media[0],
                "ad_product_key": media[1],
                "marketplace_key": market,
            }
            for column in plan.carried_columns:
                row[column] = nc.CURRENCIES[market]
            if plan.join is not None:
                for index, operand in enumerate((plan.metric.numerator, plan.metric.denominator)):
                    value = self._column(operand, media, current)
                    row[measure_alias(index)] = self._scaled(value, market)
            else:
                for index, column in enumerate(plan.measures):
                    value = self._column(column, media, current)
                    row[measure_alias(index)] = self._scaled(value, market)
            rows.append({key: row[key] for key in (*plan.group_by, *row) if key in row})
        return rows


# The approved 7-day mockup's step 1 (docs/ui/new-customers/mockup.html), as (current, prior):
# cost per new-to-brand purchase is spend / purchases, and the share NTB revenue / attributed.
_MOCKUP_DELIVERY: dict[str, dict[tuple[str, str], tuple[Decimal, Decimal]]] = {
    "ad_spend": {
        nc.DSP: (Decimal("21398.80"), Decimal(20000)),
        nc.SP: (Decimal("15918.20"), Decimal(15000)),
        nc.SB: (Decimal("7308.60"), Decimal(7000)),
        SD: (Decimal("3579.60"), Decimal(3500)),
    }
}
_MOCKUP_CONVERSION: dict[str, dict[tuple[str, str], tuple[Decimal, Decimal]]] = {
    "purchases_new_to_brand_12mo": {
        nc.DSP: (Decimal(610), Decimal(560)),
        nc.SP: (Decimal(1420), Decimal(1400)),
        nc.SB: (Decimal(390), Decimal(400)),
        SD: (Decimal(95), Decimal(100)),
    },
    "revenue_new_to_brand_12mo": {
        nc.DSP: (Decimal(25300), Decimal(22000)),
        nc.SP: (Decimal(14300), Decimal(14300)),
        nc.SB: (Decimal(8800), Decimal(9000)),
        SD: (Decimal(1900), Decimal(2000)),
    },
    "revenue_attributed": {
        nc.DSP: (Decimal(61707), Decimal(57895)),
        nc.SP: (Decimal("79444.44"), Decimal("79444.44")),
        nc.SB: (Decimal(26667), Decimal(25714)),
        SD: (Decimal(7917), Decimal(8000)),
    },
}
# Amazon DSP's three listed products are the mockup's (212, 148, 96: 35%, 24% and 16% of 606).
_MOCKUP_GATEWAY = {
    (nc.DSP, "amazon_asin:B0FICT0001"): 212,
    (nc.DSP, "amazon_asin:B0FICT0002"): 148,
    (nc.DSP, "amazon_asin:B0FICT0003"): 96,
    (nc.DSP, "amazon_asin:B0FICT0004"): 60,
    (nc.DSP, "amazon_asin:B0FICT0006"): 50,
    (nc.DSP, "amazon_asin:B0FICT0007"): 40,
    (nc.SP, "amazon_asin:B0FICT0001"): 640,
    (nc.SP, "amazon_asin:B0FICT0005"): 410,
    (nc.SB, "amazon_asin:B0FICT0002"): 120,
    (SD, "amazon_asin:B0FICT0003"): 40,
}
_MOCKUP_TITLES = {
    "amazon_asin:B0FICT0001": "Trail Bottle 750 ml",
    "amazon_asin:B0FICT0002": "Summit Pack 28 L",
    "amazon_asin:B0FICT0003": "Ridge Socks, 3 pairs",
    "amazon_asin:B0FICT0004": "Camp Mug 350 ml",
    "amazon_asin:B0FICT0005": "Trail Bottle 1 L",
    "amazon_asin:B0FICT0006": "Trail Cap",
    "amazon_asin:B0FICT0007": "Camp Towel",
}
# The month mockup's per-channel table: three rows with figures and Sponsored Display rostered
# with no row back (`not_returned`), so the roster loss is graded degraded.
_MOCKUP_CHANNEL_BUYERS = {nc.DSP: Decimal(3980), nc.SP: Decimal(4610), nc.SB: Decimal(1150)}
_MOCKUP_CHANNEL_SPEND = {
    nc.DSP: Decimal("111997.20"),
    nc.SP: Decimal(65462),
    nc.SB: Decimal(24495),
    SD: Decimal(9000),
}
# The week's per-channel figures (the LLD governs that table on the week state): the week's
# spend split across the four ad products, each with new buyers back.
_WEEK_CHANNEL_SPEND: dict[tuple[str, str], Decimal] = {
    nc.DSP: Decimal("21398.80"),
    nc.SP: Decimal("15918.20"),
    nc.SB: Decimal("7308.60"),
    SD: Decimal("1808.40"),
}
_WEEK_CHANNEL_BUYERS: dict[tuple[str, str], Decimal] = {
    nc.DSP: Decimal(940),
    nc.SP: Decimal(1090),
    nc.SB: Decimal(290),
    SD: Decimal(30),
}
_ROSTER_LABELS = {
    **nc._ROSTER_LABELS,  # pyright: ignore[reportPrivateUsage]
    SD[1]: "Sponsored Display",
}
FOUR = (nc.DSP[1], nc.SP[1], nc.SB[1], SD[1])


@dataclass(frozen=True, slots=True)
class _Titles:
    """Effective product titles for the gateway list, as dim_product would declare them."""

    def hierarchy(self, window: DateRange) -> RetailProductHierarchyResult:
        del window
        return RetailProductHierarchyResult(
            {
                asin: RetailProductIdentity(
                    asin.removeprefix("amazon_asin:"), "B0FICTPARENT", title
                )
                for asin, title in _MOCKUP_TITLES.items()
            },
            ("fictional-dim-product",),
        )


@contextmanager
def _mockup(  # noqa: PLR0913 -- the fictional port's five knobs, by keyword
    *,
    spend: Decimal | None = None,
    channel_buyers: dict[tuple[str, str], Decimal] | None = None,
    channel_spend: dict[tuple[str, str], Decimal] | None = None,
    delivering: tuple[tuple[str, str], ...] | None = None,
    conversion: dict[str, dict[tuple[str, str], tuple[Decimal, Decimal]]] | None = None,
) -> Generator[None]:
    """Bake the approved mockups' four ad products, gateway products and per-channel table.

    `delivering` keeps only those ad products' delivery, so the others have no ad spend.
    `conversion` replaces the mockup's step-1 conversions, current and prior.
    """
    spent = _MOCKUP_DELIVERY["ad_spend"]
    delivery = (
        _MOCKUP_DELIVERY
        if delivering is None
        else {"ad_spend": {media: spent[media] for media in delivering}}
    )
    with (
        patch.object(nc, "_DELIVERY", delivery),
        patch.object(nc, "_CONVERSION", conversion or _MOCKUP_CONVERSION),
        patch.object(nc, "_GATEWAY_PRODUCTS", _MOCKUP_GATEWAY),
        patch.object(nc, "_CHANNEL_SPEND", channel_spend or _MOCKUP_CHANNEL_SPEND),
        patch.object(nc, "_CHANNEL_BUYERS", channel_buyers or _MOCKUP_CHANNEL_BUYERS),
        patch.object(nc, "_ROSTER_LABELS", _ROSTER_LABELS),
        patch.object(nc, "_WEEK_SPEND", spend or nc._WEEK_SPEND),  # pyright: ignore[reportPrivateUsage]
    ):
        yield


def _publication(  # noqa: PLR0913 -- the variant and the fictional port's knobs, by keyword
    variant: str,
    *,
    buyers: Decimal | None = nc._BUYERS_COUNT,  # pyright: ignore[reportPrivateUsage]
    channel_buyers: dict[tuple[str, str], Decimal] | None = None,
    marketplaces: tuple[str, ...] = (nc.US,),
    doubled: frozenset[str] = frozenset(),
    titled: bool = False,
    now: datetime = NOW,
    **renderer: object,
) -> PresetArtifactPublicationRequest:
    """Bake one variant through the real retained source at `now`."""
    window = WINDOWS[variant]
    queries = _Queries(
        window,
        doubled=doubled,
        marketplaces=marketplaces,
        buyers=buyers,
        channel_buyers=dict(
            nc._CHANNEL_BUYERS  # pyright: ignore[reportPrivateUsage]
            if channel_buyers is None
            else channel_buyers
        ),
    )
    compiler = nc._renderer(queries, now=now, **renderer)  # type: ignore[arg-type]  # pyright: ignore[reportPrivateUsage]
    if titled:
        compiler = replace(compiler, titles=_Titles())
    source = RetainedNewCustomersPresetArtifactSource(
        coverage=nc._SourceCoverage((DateRange(COVERED_FROM, nc.PERIOD.end),)),  # pyright: ignore[reportPrivateUsage]
        coverage_requirements=new_customers_coverage_requirements(nc.CATALOG),
        temporal_policy=nc.POLICY,
        compiler=compiler,
        pointers=cast("Any", nc._Pointers()),  # pyright: ignore[reportPrivateUsage]
        clock=lambda: now,
    )
    publication = source.publication_for(PresetArtifactVariant(variant))
    assert isinstance(publication, PresetArtifactPublicationRequest), variant
    return publication


def _served(*periods: CalendarPeriod, channel: frozenset[CalendarPeriod] | None = None) -> object:
    return nc._Served(frozenset(periods), channel)  # pyright: ignore[reportPrivateUsage]


@dataclass
class _Stores:
    """New customers bodies and pointers, keyed by variant."""

    bodies: dict[str, PresetArtifactPublicationRequest]
    failing: bool = False
    reads: list[str] = field(default_factory=list[str])

    def roster(
        self, request: PresetArtifactPointerRosterRequest
    ) -> PresetArtifactPointerRosterResult:
        self.reads.append(request.page.value)
        if self.failing:
            raise RuntimeError("fictional pointer read failure")
        pointers: list[PresetArtifactPointer | None] = []
        for variant in request.variants:
            published = self.bodies.get(variant.value) if request.page.value == PAGE else None
            pointers.append(None if published is None else published.pointer_advance.pointer)
        return PresetArtifactPointerRosterResult(request, tuple(pointers))

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactBody:
        return self.bodies[pointer.variant.value].body


def _request(body: Mapping[str, object]) -> dict[str, Any]:
    event = _event(PATH, {"session_id": SESSION, **body})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
    return event


def _runtime(  # noqa: PLR0913 -- the module, its stores, the clock and the coverage
    module: _PresentationModule,
    stores: _Stores,
    *,
    now: datetime = NOW,
    covered: tuple[DateRange, ...] = (DateRange(COVERED_FROM, nc.PERIOD.end),),
) -> object:
    pages = RecordingPages(tokens=_TOKENS, clock=lambda: now)
    return module.runtime_factory(
        catalog=RecordingCatalog(),
        page_context=pages,
        page_artifacts=pages,
        parent_capability_issuance=pages,
        scope=AuthorizedAnalyticalScope(frozenset({CommerceScopeKey("amazon_retail_us")})),
        clock=pages.clock,
        token_minter=iter(_TOKENS).__next__,
        schemas=SCHEMAS,
        metric_catalog=METRIC_CATALOG,
        windows=nc._SourceCoverage(covered),  # pyright: ignore[reportPrivateUsage]
        preset_artifact_bodies=stores,
        preset_artifact_pointers=stores,
    )


def _serve(
    module: _PresentationModule,
    stores: _Stores,
    body: Mapping[str, object] | None = None,
    **runtime: Any,
) -> dict[str, Any]:
    return module.handle(_request(body or {}), _runtime(module, stores, **runtime))


def _ok(response: dict[str, Any]) -> dict[str, Any]:
    assert response["statusCode"] == 200, response["body"]
    return cast("dict[str, Any]", _body(response))


def test_a_rolling_preset_serves_its_baked_body_with_a_version_2_control(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    stores = _Stores({"7d": _publication("7d")})
    body = _ok(_serve(presentation_module, stores, SEVEN_DAY_SELECTION))
    temporal = body["temporal"]
    assert temporal["version"] == 2
    assert [preset["id"] for preset in temporal["presets"]] == [
        "7d",
        "30d",
        "last_complete_week",
        "last_complete_month",
    ]
    assert temporal["comparison"]["status"] == "available"
    page = body["page"]
    assert (page["page_id"], page["route"], page["kind"], page["artifact_type"]) == (
        PAGE,
        "/new-customers/",
        "new_customers",
        "NewCustomersArtifact",
    )
    assert page["artifact"]["version"] == 2
    assert page["page_context_label"] == "New customers \u00b7 Sep 14\u201320, 2026"
    # One read: the page's own pointers.
    assert stores.reads == [PAGE]


def test_an_empty_lake_serves_the_designed_absence_for_every_new_customers_preset(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """A new deployment: no bake and no source coverage. `NOW` is Sep 23, 2026."""
    stores = _Stores({})
    # The bare request opens Last full week (OD-UX-6), which reads as its calendar-period absence.
    bare = _ok(_serve(presentation_module, stores, covered=()))
    week = _ok(_serve(presentation_module, stores, WEEK_SELECTION, covered=()))
    assert bare["reason_code"] == week["reason_code"] == "no_complete_period_measured"
    default = _ok(_serve(presentation_module, stores, SEVEN_DAY_SELECTION, covered=()))
    assert (default["kind"], default["reason_code"]) == ("window_unavailable", "preset_unavailable")
    # ABS-NC-1 (`docs/ui/page-absence-copy.md`): the source never delivered, so no other window.
    expected = absence_message(
        AbsenceTrigger.NEW_CUSTOMERS_WINDOW_NOT_PUBLISHED,
        period_label=None,
        other_window_ready=False,
    )
    assert default["message"] == expected
    temporal = default["temporal"]
    assert temporal["version"] == 2
    assert temporal["resolved"]["window"] == {"start": "2026-09-17", "end": "2026-09-23"}
    assert {preset["availability"] for preset in temporal["presets"]} == {"unavailable"}
    thirty = _ok(
        _serve(
            presentation_module,
            stores,
            {"temporal_selection": {"kind": "preset", "preset_id": "30d"}},
            covered=(),
        )
    )
    assert (thirty["reason_code"], thirty["message"]) == ("preset_unavailable", expected)
    month = _ok(_serve(presentation_module, stores, MONTH_SELECTION, covered=()))
    assert month["reason_code"] == "no_complete_period_measured"


def _served_window(window: Mapping[str, str] | None) -> DateRange | None:
    if window is None:
        return None
    return DateRange(date.fromisoformat(window["start"]), date.fromisoformat(window["end"]))


def test_each_pointer_names_its_window_so_the_control_dates_every_baked_preset(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """ADR-116 decision 4: the date control names each baked preset's dates, as the mockups do."""
    every = {variant: _publication(variant) for variant in WINDOWS}
    for variant, publication in every.items():
        pointer = publication.pointer_advance.pointer
        payload = json.loads(publication.body.payload_json)
        assert pointer.resolved_window == _served_window(payload["period"]), variant
        assert pointer.comparison_window == _served_window(payload["prior_period"]), variant
    presets = _ok(_serve(presentation_module, _Stores(every), SEVEN_DAY_SELECTION))["temporal"][
        "presets"
    ]
    shown = {preset["id"]: (preset["window"], preset["period_label"]) for preset in presets}
    assert shown == {
        "7d": ({"start": "2026-09-14", "end": "2026-09-20"}, None),
        "30d": ({"start": "2026-08-22", "end": "2026-09-20"}, None),
        "last_complete_week": (
            {"start": "2026-09-14", "end": "2026-09-20"},
            "Week of 14\u201320 Sep 2026",
        ),
        "last_complete_month": ({"start": "2026-08-01", "end": "2026-08-31"}, "August 2026"),
    }
    # Paired: pointers written before they carried windows name no dates for the other presets.
    bare = {
        variant: replace(
            publication,
            pointer_advance=replace(
                publication.pointer_advance,
                pointer=replace(
                    publication.pointer_advance.pointer,
                    resolved_window=None,
                    comparison_window=None,
                ),
            ),
        )
        for variant, publication in every.items()
    }
    presets = _ok(_serve(presentation_module, _Stores(bare), SEVEN_DAY_SELECTION))["temporal"][
        "presets"
    ]
    assert [preset["window"] for preset in presets[1:]] == [None, None, None]


@pytest.mark.parametrize(
    "body",
    [
        {"temporal_selection": {"kind": "custom", "start": "2026-08-01", "end": "2026-08-07"}},
        {"temporal_selection": {"kind": "preset", "preset_id": "14d"}},
        {"temporal_selection": {"kind": "preset", "preset_id": "last_complete_quarter"}},
        {"channel": "amazon_dsp"},
        {"marketplace": "amazon_us"},
        {"comparator": "prior"},
    ],
)
def test_every_field_beyond_the_session_and_a_declared_preset_is_refused(
    presentation_module: _PresentationModule,  # noqa: F811
    body: dict[str, object],
) -> None:
    stores = _Stores({"7d": _publication("7d")})
    with pytest.raises(ValidationError):
        _serve(presentation_module, stores, body)
    # Paired: the same stores serve the bare request.
    assert _serve(presentation_module, stores)["statusCode"] == 200


def _section(body: dict[str, Any]) -> dict[str, Any]:
    (market,) = body["page"]["artifact"]["markets"]
    return cast("dict[str, Any]", market["new_buyers"])


def _entry(body: dict[str, Any], slot: str) -> dict[str, Any]:
    return next(
        entry
        for entry in body["page"]["artifact"]["evidence_status"]["entries"]
        if entry["slot"] == slot
    )


def test_a_baked_pending_section_turns_missing_at_serve_time_with_no_rebake(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """ADR-107: the tiles' and the per-channel table's own expected dates, at the serving date."""
    # Baked on 22 Sep, the day the week's deliveries are expected after: both pending.
    baked_on = datetime(2026, 9, 22, 12, tzinfo=UTC)
    tiles = _Stores(
        {"last_complete_week": _publication("last_complete_week", now=baked_on, served=_served())}
    )
    per_channel = _Stores(
        {
            "last_complete_week": _publication(
                "last_complete_week", now=baked_on, served=_served(WEEK, channel=frozenset())
            )
        }
    )
    for stores, path, slot in (
        (tiles, (), "new-buyers:amazon_us"),
        (per_channel, ("per_channel",), "new-buyers-per-channel:amazon_us"),
    ):
        # Paired: served the same day, the bake's pending state stands.
        same_day = _ok(_serve(presentation_module, stores, WEEK_SELECTION, now=baked_on))
        later = _ok(_serve(presentation_module, stores, WEEK_SELECTION, now=NOW))
        early_section, late_section = _section(same_day), _section(later)
        for key in path:
            early_section, late_section = early_section[key], late_section[key]
        assert early_section["state"] == "pending"
        assert early_section["text"].endswith("are expected after 22 Sep 2026.")
        assert late_section["state"] == "missing"
        assert late_section["text"].endswith("didn't arrive.")
        # The coverage sheet reads the re-decided text, never the baked one.
        assert _entry(later, slot)["reason"] == late_section["text"]
        assert _entry(same_day, slot)["reason"] == early_section["text"]
    # The re-decided body equals what a bake on the serving date writes, field for field.
    for stores, served_periods in (
        (tiles, _served()),
        (per_channel, _served(WEEK, channel=frozenset())),
    ):
        fresh = json.loads(
            _publication("last_complete_week", now=NOW, served=served_periods).body.payload_json
        )
        served = _ok(_serve(presentation_module, stores, WEEK_SELECTION, now=NOW))["page"]
        assert _without_context(served["artifact"]) == _without_context(fresh)


def test_a_re_decided_section_without_its_coverage_entry_is_refused() -> None:
    """NC-GATE-006: the section line and the coverage sheet never name one window two ways."""
    baked_on = datetime(2026, 9, 22, 12, tzinfo=UTC)
    for served_periods, slot in (
        (_served(), "new-buyers:amazon_us"),
        (_served(WEEK, channel=frozenset()), "new-buyers-per-channel:amazon_us"),
    ):
        baked = json.loads(
            _publication(
                "last_complete_week", now=baked_on, served=served_periods
            ).body.payload_json
        )
        # Paired: with its entry the body re-decides, and the entry reads the section's new text.
        resolved = cast("dict[str, Any]", resolve_new_customers_payload(baked, NOW.date()))
        (entry,) = (item for item in resolved["evidence_status"]["entries"] if item["slot"] == slot)
        assert entry["reason"].endswith("didn't arrive."), slot
        stripped = json.loads(json.dumps(baked))
        status = stripped["evidence_status"]
        status["entries"] = [item for item in status["entries"] if item["slot"] != slot]
        with pytest.raises(ValueError, match="exactly one current coverage entry"):
            resolve_new_customers_payload(stripped, NOW.date())
        # The same refusal the handler turns into the malformed-artifact error, never a body
        # whose section and coverage sheet disagree.
        doubled = json.loads(json.dumps(baked))
        twice = [item for item in doubled["evidence_status"]["entries"] if item["slot"] == slot]
        doubled["evidence_status"]["entries"].extend(twice)
        with pytest.raises(ValueError, match="exactly one current coverage entry"):
            resolve_new_customers_payload(doubled, NOW.date())


def _without_context(artifact: dict[str, Any]) -> dict[str, Any]:
    """Drop the page-context fields, which the serving envelope sets (plan D5)."""
    return {
        key: value
        for key, value in artifact.items()
        if key not in {"page_context_expires_at", "page_context_label", "starter_questions"}
    }


def test_the_artifact_s_page_context_is_the_envelope_s_and_the_bake_names_the_same_window(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """Plan D5: the envelope renders; the artifact's own fields equal it, so drift fails here."""
    for variant, selection in (("7d", SEVEN_DAY_SELECTION), ("last_complete_week", WEEK_SELECTION)):
        publication = _publication(variant)
        baked = json.loads(publication.body.payload_json)
        page = _ok(_serve(presentation_module, _Stores({variant: publication}), selection))["page"]
        envelope = (page["page_context_label"], page["starter_questions"])
        assert envelope == (baked["page_context_label"], baked["starter_questions"]), variant
        artifact = page["artifact"]
        assert (artifact["page_context_label"], artifact["starter_questions"]) == envelope
        assert artifact["page_context_expires_at"] == page["page_context_expires_at"]
        assert baked["page_context_expires_at"] is None
    # Control: an empty body offers no starter; the served artifact follows the envelope.
    with _delivery({}):
        empty = _publication("7d")
    assert json.loads(empty.body.payload_json)["starter_questions"]
    page = _ok(_serve(presentation_module, _Stores({"7d": empty}), SEVEN_DAY_SELECTION))["page"]
    assert page["starter_questions"] == page["artifact"]["starter_questions"] == []


@contextmanager
def _delivery(spend: dict[tuple[str, str], tuple[Decimal, Decimal]]) -> Generator[None]:
    with patch.object(nc, "_DELIVERY", {"ad_spend": spend}):
        yield


def test_a_calendar_preset_with_no_complete_period_serves_the_designed_state(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    stores = _Stores({"7d": _publication("7d")})
    # Nothing in August is covered, so no complete month has been measured.
    none = _ok(
        _serve(
            presentation_module,
            stores,
            MONTH_SELECTION,
            covered=(DateRange(date(2026, 9, 2), nc.PERIOD.end),),
        )
    )
    assert (none["kind"], none["reason_code"]) == (
        "window_unavailable",
        "no_complete_period_measured",
    )
    assert none["message"].startswith("No complete month has been measured yet.")
    assert none["expected_after"] is not None
    # Paired: a covered August is a period whose bake is pending, not "no complete period".
    pending = _ok(_serve(presentation_module, stores, MONTH_SELECTION))
    assert pending["reason_code"] == "preset_unavailable"
    # ABS-NC-2 with ABS-ALT (`docs/ui/page-absence-copy.md`): another preset is published.
    assert pending["message"] == absence_message(
        AbsenceTrigger.NEW_CUSTOMERS_PERIOD_NOT_PUBLISHED,
        period_label="August 2026",
        other_window_ready=True,
    )
    assert pending["temporal"]["presets"][-1]["period_label"] == "August 2026"


def test_a_failing_pointer_read_reaches_the_entrypoint_s_safe_500(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    baked = {"7d": _publication("7d")}
    with patch.object(
        presentation_module.module, "RUNTIME", _runtime(presentation_module, _Stores(baked))
    ):
        served = presentation_module.handler(_request({}), object())
    assert served["statusCode"] == 200
    failing = _Stores(baked, failing=True)
    with patch.object(
        presentation_module.module, "RUNTIME", _runtime(presentation_module, failing)
    ):
        response = presentation_module.handler(_request({}), object())
    assert response["statusCode"] == 500
    assert _body(response) == {
        "code": "ADLY-UNEXPECTED-0000",
        "error": "The request could not be completed. The issue has been recorded.",
        "error_class": "invariant",
    }


def browser_fixture_responses(module: _PresentationModule) -> dict[str, dict[str, Any]]:
    """Serve every New customers response the browser tests and the harness decode (plan B.2)."""
    with _mockup():
        rolling = _publication("7d", titled=True)
        thirty = _publication("30d", titled=True)
    four = nc._Rosters(keys=FOUR)  # pyright: ignore[reportPrivateUsage]
    with _mockup(spend=Decimal(46434), channel_spend=_WEEK_CHANNEL_SPEND):
        week = _publication(
            "last_complete_week",
            buyers=Decimal(2180),
            channel_buyers=_WEEK_CHANNEL_BUYERS,
            titled=True,
            rosters=four,
        )
    with _mockup(spend=Decimal(203600)):
        month = _publication(
            "last_complete_month",
            buyers=Decimal(9240),
            titled=True,
            served=_served(MONTH),
            rosters=nc._Rosters(keys=FOUR),  # pyright: ignore[reportPrivateUsage]
        )
    every = {"7d": rolling, "30d": thirty, "last_complete_week": week, "last_complete_month": month}
    stores = _Stores(every)

    def week_state(
        delivering: tuple[tuple[str, str], ...] | None = None, **knobs: Any
    ) -> dict[str, Any]:
        knobs = {"channel_buyers": _WEEK_CHANNEL_BUYERS, "rosters": four, **knobs}
        with _mockup(
            spend=Decimal(46434), channel_spend=_WEEK_CHANNEL_SPEND, delivering=delivering
        ):
            body = _publication("last_complete_week", titled=True, **knobs)
        return _body(_serve(module, _Stores({**every, "last_complete_week": body}), WEEK_SELECTION))

    responses = {
        "new-customers-7d-v2.json": _body(_serve(module, stores, SEVEN_DAY_SELECTION)),
        "new-customers-30d-v2.json": _body(
            _serve(module, stores, {"temporal_selection": {"kind": "preset", "preset_id": "30d"}})
        ),
        "new-customers-week-v2.json": _body(_serve(module, stores, WEEK_SELECTION)),
        "new-customers-month-v2.json": _body(_serve(module, stores, MONTH_SELECTION)),
        # NC-5: the platform cell didn't come back. The platform count deduplicates the same
        # shoppers, so it's at least each channel's: no rostered channel's row came back either
        # (NC-GATE-102), and every row reads "too few shoppers, or none" too.
        "new-customers-tiles-not-returned-v2.json": week_state(buyers=None, channel_buyers={}),
        # NC-6: the platform cell came back with 0 new buyers (owner-ratified copy), so every
        # rostered channel's row came back with 0 too: the table reads the row copy throughout.
        "new-customers-tile-no-new-buyers-v2.json": week_state(
            buyers=Decimal(0), channel_buyers=dict.fromkeys(_WEEK_CHANNEL_BUYERS, Decimal(0))
        ),
        # NC-7: 0 new buyers on Sponsored Brands beside a numeric row and a row not returned.
        "new-customers-row-no-new-buyers-v2.json": week_state(
            buyers=Decimal(2180),
            channel_buyers={nc.DSP: Decimal(940), nc.SP: Decimal(1090), nc.SB: Decimal(0)},
        ),
        # NC-8: Sponsored Display had no ad spend in the week, so it's off the roster and out of
        # step 1, and its returned new buyers read the no-spend text on the cost.
        "new-customers-no-ad-spend-v2.json": week_state(
            delivering=(nc.DSP, nc.SP, nc.SB),
            buyers=Decimal(2180),
            channel_buyers={
                nc.DSP: Decimal(940),
                nc.SP: Decimal(1090),
                nc.SB: Decimal(290),
                SD: Decimal(30),
            },
            rosters=nc._Rosters(keys=(nc.DSP[1], nc.SP[1], nc.SB[1])),  # pyright: ignore[reportPrivateUsage]
        ),
        # NC-9: an empty roster and no per-channel row.
        "new-customers-no-channels-v2.json": week_state(
            buyers=Decimal(2180),
            channel_buyers={},
            rosters=nc._Rosters(keys=()),  # pyright: ignore[reportPrivateUsage]
        ),
        # NC-10 and NC-11: the per-channel delivery is pending, then past due.
        "new-customers-per-channel-pending-v2.json": _per_channel_awaiting(
            module, every, NOW - timedelta(days=1)
        ),
        "new-customers-per-channel-missing-v2.json": _per_channel_awaiting(module, every, NOW),
        # NC-12 and NC-13: the platform delivery is pending, then past due.
        "new-customers-tiles-pending-v2.json": _tiles_awaiting(
            module, every, NOW - timedelta(days=1)
        ),
        "new-customers-tiles-missing-v2.json": _tiles_awaiting(module, every, NOW),
        # NC-14 and NC-15: the source tests' state, where Sponsored Brands delivered with no
        # conversion row (true zeros and the step-1 absences) and has no gateway purchases.
        "new-customers-step-1-absences-v2.json": _body(
            _serve(module, _Stores({"7d": _publication("7d")}), SEVEN_DAY_SELECTION)
        ),
        "new-customers-gateway-not-covered-v2.json": _gateway_uncovered(
            module, {nc.PERIOD.start: 1}
        ),
        "new-customers-gateway-not-measured-v2.json": _gateway_uncovered(
            module, {nc.PERIOD.start + timedelta(days=offset): 1 for offset in range(7)}
        ),
        # NC-16: two marketplaces in two currencies, each section once per marketplace.
        "new-customers-two-marketplaces-v2.json": _two_marketplaces(module),
        # NC-23: a rolling preset where no share moved by the threshold (NC-GATE-101's control).
        "new-customers-rolling-quiet-v2.json": _body(
            _serve(module, _Stores({"7d": _quiet_7d()}), SEVEN_DAY_SELECTION)
        ),
        # NC-17: no complete month measured yet, and a month whose bake is pending.
        "new-customers-no-period-v2.json": _body(
            _serve(
                module,
                _Stores({"7d": rolling}),
                MONTH_SELECTION,
                covered=(DateRange(date(2026, 9, 2), nc.PERIOD.end),),
            )
        ),
        "new-customers-pending-v2.json": _body(
            _serve(module, _Stores({"7d": rolling}), MONTH_SELECTION)
        ),
        # NC-22: the entrypoint's safe error when the pointer read fails, with its status.
        "new-customers-error-v2.json": _entrypoint_error(module, every),
    }
    # NC-18: no ad product delivered in the window: the bake's outcome is `empty`.
    with _delivery({}):
        responses["new-customers-empty-v2.json"] = _body(
            _serve(module, _Stores({"7d": _publication("7d")}), SEVEN_DAY_SELECTION)
        )
    return responses


def _quiet_7d() -> PresetArtifactPublicationRequest:
    """Build the 7-day mockup with Amazon DSP's prior share unchanged."""
    revenue = _MOCKUP_CONVERSION["revenue_new_to_brand_12mo"]
    conversion = {
        **_MOCKUP_CONVERSION,
        # 25300 / 61707 and 23737 / 57895 both round to 41.00%.
        "revenue_new_to_brand_12mo": {**revenue, nc.DSP: (Decimal(25300), Decimal(23737))},
    }
    with _mockup(conversion=conversion):
        return _publication("7d", titled=True)


def test_a_calendar_preset_s_quiet_insight_says_nothing_was_compared(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """NC-GATE-101: week and month compared nothing, so the slot never claims a prior window."""
    silence = next(
        contract.silence_condition
        for _, contract in load_insight_contracts(nc.ROOT)
        if contract.id is InsightSlotId.PORTFOLIO_ACQUISITION
    )
    assert "equal prior window" in silence
    stores = _Stores(
        {
            "7d": _quiet_7d(),
            "last_complete_week": _publication("last_complete_week"),
            "last_complete_month": _publication("last_complete_month", served=_served(MONTH)),
        }
    )
    for selection in (WEEK_SELECTION, MONTH_SELECTION):
        artifact = _ok(_serve(presentation_module, stores, selection))["page"]["artifact"]
        assert artifact["prior_period"] is None
        (slot,) = artifact["insights"]
        assert (slot["state"], slot["absence_class"], slot["absence_reason"]) == (
            "silent",
            "evaluated-quiet",
            "No comparison for calendar periods.",
        ), selection
    # Paired control: a rolling preset where nothing moved still serves the silence condition.
    artifact = _ok(_serve(presentation_module, stores, SEVEN_DAY_SELECTION))["page"]["artifact"]
    assert artifact["prior_period"] is not None
    assert artifact["insight_cards"] == []
    (slot,) = artifact["insights"]
    assert (slot["state"], slot["absence_class"], slot["absence_reason"]) == (
        "silent",
        "evaluated-quiet",
        silence,
    )


def _entrypoint_error(
    module: _PresentationModule, every: dict[str, PresetArtifactPublicationRequest]
) -> dict[str, Any]:
    with patch.object(module.module, "RUNTIME", _runtime(module, _Stores(every, failing=True))):
        response = module.handler(_request({}), object())
    return {"status": response["statusCode"], "body": _body(response)}


def _per_channel_awaiting(
    module: _PresentationModule, every: dict[str, PresetArtifactPublicationRequest], now: datetime
) -> dict[str, Any]:
    with _mockup(spend=Decimal(46434), channel_spend=_WEEK_CHANNEL_SPEND):
        body = _publication(
            "last_complete_week",
            buyers=Decimal(2180),
            titled=True,
            served=_served(WEEK, channel=frozenset()),
            now=datetime(2026, 9, 22, 12, tzinfo=UTC),
        )
    stores = _Stores({**every, "last_complete_week": body})
    return _body(_serve(module, stores, WEEK_SELECTION, now=now))


def _tiles_awaiting(
    module: _PresentationModule, every: dict[str, PresetArtifactPublicationRequest], now: datetime
) -> dict[str, Any]:
    with _mockup(spend=Decimal(46434), channel_spend=_WEEK_CHANNEL_SPEND):
        body = _publication(
            "last_complete_week",
            buyers=Decimal(2180),
            titled=True,
            served=_served(),
            now=datetime(2026, 9, 22, 12, tzinfo=UTC),
        )
    stores = _Stores({**every, "last_complete_week": body})
    return _body(_serve(module, stores, WEEK_SELECTION, now=now))


def _gateway_uncovered(
    module: _PresentationModule, overrides: dict[date, int | None]
) -> dict[str, Any]:
    with _mockup():
        body = _publication("7d", titled=True, coverage=nc._Coverage(overrides=overrides))  # pyright: ignore[reportPrivateUsage]
    return _body(_serve(module, _Stores({"7d": body}), SEVEN_DAY_SELECTION))


def _two_marketplaces(module: _PresentationModule) -> dict[str, Any]:
    labels = replace(
        nc.LABELS,
        marketplaces=(nc.UK, nc.US),
        marketplace_labels={nc.UK: "UK", nc.US: "US"},
    )
    with _mockup():
        body = _publication(
            "7d",
            titled=True,
            marketplaces=(nc.UK, nc.US),
            doubled=frozenset({nc.UK}),
            labels=labels,
        )
    return _body(_serve(module, _Stores({"7d": body}), SEVEN_DAY_SELECTION))


def test_the_browser_fixtures_are_the_servers_own_new_customers_responses(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    responses = browser_fixture_responses(presentation_module)
    for name, served in responses.items():
        committed = json.loads((_FIXTURES / name).read_text())
        assert committed == json.loads(json.dumps(served)), name
    (declared,) = next(
        page for page in load_v2_page_schemas(nc.ROOT).pages if page.page_id == PAGE
    ).artifacts
    served_outcomes = {
        body["page"]["artifact"]["outcome"] for body in responses.values() if "page" in body
    }
    # The materializer bakes `complete` and `empty`; `insufficient` is declared by the approved
    # FEAT-004 contract (the source opens only on all three) and no bake produces it, so it's
    # the one declared outcome no response carries. A change to either set fails here.
    assert set(declared.outcomes) - served_outcomes == {NewCustomersOutcome.INSUFFICIENT.value}
    assert served_outcomes == {"complete", "empty"}
