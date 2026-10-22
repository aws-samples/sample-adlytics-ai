# SPDX-License-Identifier: MIT-0
"""The Scorecard serving path (UI slice 9a item A7): baked bodies, serve-time links, v2 control.

Bodies are baked by the real Scorecard source and renderer over fictional figures; the
handler serves them through `POST /api/presentation/scorecard`. Each passing case sits beside
the control that must fail on the guarded defect.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any, cast
from unittest.mock import patch

import pytest
import test_network_scorecard as scorecard_tests
from publication_support import StubPublications
from test_network_scorecard import (
    _Coverage,  # pyright: ignore[reportPrivateUsage]
    _source,  # pyright: ignore[reportPrivateUsage]
)
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

from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from adapters.jobs.preset_artifact_publisher import scorecard_source
from adapters.outbound.telemetry.reporting import OperationLog
from domain.presentation.absence_copy import AbsenceTrigger, absence_message
from domain.presentation.preset_artifacts import (
    PresetArtifactBody,
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerRosterRequest,
    PresetArtifactPointerRosterResult,
    PresetArtifactPublicationRequest,
    PresetArtifactVariant,
)
from domain.presentation.scorecard import ScorecardOutcome, network_scorecard_artifact_payload
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.identity import CommerceScopeKey
from domain.shared_kernel.errors import ValidationError
from domain.shared_kernel.values import DateRange
from ports.outbound.publication_selection import (
    PresentationEvidenceInspectionRequest,
    PresentationEvidenceInspectionResult,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-100", "ADR-116", "ADR-126")]

PATH = "/api/presentation/scorecard"
_TOKENS = tuple(f"{letter * 43}" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
COVERED = (DateRange(date(2026, 7, 1), date(2026, 9, 15)),)
CAMPAIGN_WINDOW = DateRange(date(2026, 9, 9), date(2026, 9, 15))
_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "frontend/test/fixtures"


def _bake(*variants: str) -> dict[str, PresetArtifactPublicationRequest]:
    source, _ = _source(COVERED)
    baked: dict[str, PresetArtifactPublicationRequest] = {}
    for variant in variants:
        publication = source.publication_for(PresetArtifactVariant(variant))
        assert isinstance(publication, PresetArtifactPublicationRequest), variant
        baked[variant] = publication
    return baked


@dataclass
class _Stores:
    """Scorecard bodies and pointers, plus Campaigns and Retail health pointers for links."""

    scorecard: dict[str, PresetArtifactPublicationRequest]
    targets: dict[tuple[str, str], DateRange] = field(
        default_factory=dict[tuple[str, str], DateRange]
    )
    failing: frozenset[str] = frozenset()
    reads: list[str] = field(default_factory=list[str])

    def _target(self, page: str, variant: str) -> PresetArtifactPointer | None:
        window = self.targets.get((page, variant))
        if window is None:
            return None
        template = next(iter(self.scorecard.values())).pointer_template.complete(None).pointer
        return replace(
            template,
            page=PresetArtifactPage(page),
            variant=PresetArtifactVariant(variant),
            resolved_window=window,
            comparison_window=None,
        )

    def roster(
        self, request: PresetArtifactPointerRosterRequest
    ) -> PresetArtifactPointerRosterResult:
        self.reads.append(request.page.value)
        if request.page.value in self.failing:
            raise RuntimeError("fictional pointer read failure")
        pointers: list[PresetArtifactPointer | None] = []
        for variant in request.variants:
            if request.page.value == "scorecard":
                published = self.scorecard.get(variant.value)
                pointers.append(
                    None if published is None else published.pointer_template.complete(None).pointer
                )
            else:
                pointers.append(self._target(request.page.value, variant.value))
        return PresetArtifactPointerRosterResult(request, tuple(pointers))

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactBody:
        return self.scorecard[pointer.variant.value].body


def _request(body: dict[str, object]) -> dict[str, Any]:
    event = _event(PATH, {"session_id": SESSION, **body})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
    return event


def _runtime(
    module: _PresentationModule, stores: _Stores, covered: tuple[DateRange, ...]
) -> object:
    pages = RecordingPages(tokens=_TOKENS)
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
        windows=_Coverage(covered),
        preset_artifact_bodies=stores,
        preset_artifact_pointers=stores,
    )


def _serve(  # noqa: PLR0913 -- the module, its stores, the body and the coverage, by keyword
    module: _PresentationModule,
    stores: _Stores,
    body: dict[str, object],
    *,
    covered: tuple[DateRange, ...] = COVERED,
) -> dict[str, Any]:
    return module.handle(_request(body), _runtime(module, stores, covered))


def serve_through_entrypoint(
    module: _PresentationModule, stores: _Stores, body: dict[str, object]
) -> dict[str, Any]:
    """Answer the request through the Lambda entrypoint (`handler`) over these stores.

    The entrypoint is the one that turns an uncaught failure into its safe 500 payload, so the
    page error state's response comes from here, never from the payload builder alone.
    """
    with patch.object(module.module, "RUNTIME", _runtime(module, stores, COVERED)):
        return module.handler(_request(body), object())


def _ok(response: dict[str, Any]) -> dict[str, Any]:
    assert response["statusCode"] == 200, response["body"]
    return cast("dict[str, Any]", _body(response))


def test_a_rolling_preset_serves_its_baked_body_with_a_version_2_control(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    stores = _Stores(_bake("7d", "30d", "last_complete_month"))
    body = _ok(_serve(presentation_module, stores, {}))
    temporal = body["temporal"]
    assert temporal["version"] == 2
    assert [preset["id"] for preset in temporal["presets"]] == [
        "7d",
        "30d",
        "last_complete_week",
        "last_complete_month",
    ]
    week = temporal["presets"][2]
    assert (week["availability"], week["reason_text"]) == ("unavailable", "Not available yet")
    assert temporal["presets"][3]["period_label"] == "August 2026"
    assert temporal["comparison"]["status"] == "available"
    page = body["page"]
    assert (page["page_id"], page["route"], page["artifact_type"]) == (
        "scorecard",
        "/scorecard/",
        "NetworkScorecardArtifact",
    )
    assert page["page_context_label"].startswith("Scorecard \u00b7 ")
    assert len(page["starter_questions"]) == 2
    # The page reads its own, Campaigns' and Retail health's pointers once each.
    assert sorted(stores.reads) == ["campaigns", "retail-health", "scorecard"]


def test_channel_links_name_what_campaigns_serves_and_fall_back_without_a_pointer(
    presentation_module: _PresentationModule,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported: list[tuple[str, str]] = []

    def report(event: OperationLog, logger: object) -> None:
        del logger
        reported.append((event.phase, event.outcome))

    monkeypatch.setattr(presentation_module.module, "report_operation", report)
    baked = _bake("7d")
    dated = _Stores(
        baked,
        {
            ("campaigns", "7d"): CAMPAIGN_WINDOW,
            ("retail-health", "7d"): DateRange(date(2026, 9, 5), date(2026, 9, 11)),
        },
    )
    rows = _ok(_serve(presentation_module, dated, {}))["page"]["artifact"]["cards"][0][
        "channel_table"
    ]["rows"]
    # Every target named its dates, so no fallback was reported.
    assert ("link_dates", "no_window") not in reported
    assert ("pointer_read", "failed") not in reported
    link = next(row["link"] for row in rows if row["channel"] == "amazon_sponsored_products")
    # 7d is Campaigns' default, so the URL leaves the preset out.
    assert link["href"] == "/campaigns/?channel=amazon_sponsored_products"
    assert link["text"] == "Open \u00b7 Sep 9\u201315"
    # Paired: with no Campaigns pointer the link names its latest preset instead.
    undated = _ok(_serve(presentation_module, _Stores(baked), {}))
    rows = undated["page"]["artifact"]["cards"][0]["channel_table"]["rows"]
    assert {row["link"]["text"] for row in rows} == {"Open \u00b7 latest 7 days"}
    assert ("link_dates", "no_window") in reported
    # A failed Campaigns read never fails the page, and is reported.
    failing = _Stores(baked, failing=frozenset({"campaigns"}))
    rows = _ok(_serve(presentation_module, failing, {}))["page"]["artifact"]["cards"][0][
        "channel_table"
    ]["rows"]
    assert {row["link"]["text"] for row in rows} == {"Open \u00b7 latest 7 days"}
    assert ("pointer_read", "failed") in reported


def test_a_calendar_preset_campaigns_doesnt_declare_links_to_its_default(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    stores = _Stores(_bake("last_complete_month"), {("campaigns", "7d"): CAMPAIGN_WINDOW})
    body = _ok(
        _serve(
            presentation_module,
            stores,
            {"temporal_selection": {"kind": "preset", "preset_id": "last_complete_month"}},
        )
    )
    assert body["temporal"]["resolved"]["period_label"] == "August 2026"
    assert body["temporal"]["comparison"]["reason_code"] == "calendar_period_selected"
    page = body["page"]
    assert page["page_context_label"] == "Scorecard \u00b7 August 2026"
    # A calendar period asks no comparison question.
    assert page["starter_questions"] == ["Which channel had the highest ROAS in August 2026?"]
    link = page["artifact"]["cards"][0]["channel_table"]["rows"][0]["link"]
    assert link["href"].startswith("/campaigns/?channel=")
    assert link["text"] == "Open \u00b7 Sep 9\u201315"


@pytest.mark.parametrize(
    "body",
    [
        {"temporal_selection": {"kind": "custom", "start": "2026-08-01", "end": "2026-08-07"}},
        {"temporal_selection": {"kind": "preset", "preset_id": "14d"}},
        {"temporal_selection": {"kind": "preset", "preset_id": "last_complete_quarter"}},
        {"channel": "amazon_dsp"},
        {"attribution_model": "linear_v1"},
        {"comparator": "prior"},
        {"filter_id": "amazon_dsp"},
    ],
)
def test_every_field_beyond_the_session_and_a_declared_preset_is_refused(
    presentation_module: _PresentationModule,  # noqa: F811
    body: dict[str, object],
) -> None:
    stores = _Stores(_bake("7d"))
    with pytest.raises(ValidationError):
        _serve(presentation_module, stores, body)
    # Paired: the same stores serve the bare request.
    assert _serve(presentation_module, stores, {})["statusCode"] == 200


def test_a_calendar_preset_with_no_complete_period_serves_the_designed_state(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """The handler's clock is Aug 19, 2026: the latest complete month is July."""
    stores = _Stores(_bake("7d"))
    selection = {"temporal_selection": {"kind": "preset", "preset_id": "last_complete_month"}}
    module = presentation_module

    def serve(covered: tuple[DateRange, ...]) -> dict[str, Any]:
        pages = RecordingPages(tokens=_TOKENS)
        event = _event(PATH, {"session_id": SESSION, **selection})
        event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
        return _ok(
            module.handle(
                event,
                presentation_module.runtime_factory(
                    catalog=RecordingCatalog(),
                    page_context=pages,
                    page_artifacts=pages,
                    parent_capability_issuance=pages,
                    scope=AuthorizedAnalyticalScope(
                        frozenset({CommerceScopeKey("amazon_retail_us")})
                    ),
                    clock=pages.clock,
                    token_minter=iter(_TOKENS).__next__,
                    schemas=SCHEMAS,
                    metric_catalog=METRIC_CATALOG,
                    windows=_Coverage(covered),
                    preset_artifact_bodies=stores,
                    preset_artifact_pointers=stores,
                ),
            )
        )

    none = serve((DateRange(date(2026, 7, 5), date(2026, 8, 18)),))
    assert none["kind"] == "window_unavailable"
    assert none["reason_code"] == "no_complete_period_measured"
    assert none["message"].startswith("No complete month has been measured yet.")
    assert none["expected_after"] is not None
    month = none["temporal"]["presets"][-1]
    assert (month["reason_text"], month["period_label"]) == ("Not measured yet", "July 2026")
    # Paired: a covered July is a period whose bake is pending, not "no complete period".
    pending = serve((DateRange(date(2026, 6, 1), date(2026, 8, 18)),))
    assert pending["reason_code"] == "preset_unavailable"
    # Temporal contract rule 3: the server writes the pending window's text (gate F7), chosen
    # from the absence catalog by its trigger (ABS-SC-2, with ABS-ALT: 7d is published).
    assert pending["message"] == absence_message(
        AbsenceTrigger.SCORECARD_PERIOD_NOT_PUBLISHED,
        period_label="July 2026",
        other_window_ready=True,
    )
    assert pending["expected_after"] is None
    assert pending["temporal"]["presets"][-1]["reason_text"] == "Not available yet"


def test_an_empty_lake_serves_the_designed_absence_for_every_scorecard_preset(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """A new deployment: no bake and no source coverage. The clock is Aug 19, 2026."""
    stores = _Stores({})
    default = _ok(_serve(presentation_module, stores, {}, covered=()))
    assert (default["kind"], default["reason_code"]) == ("window_unavailable", "preset_unavailable")
    # ABS-SC-1 (`docs/ui/page-absence-copy.md`): the source never delivered, so no other window.
    expected = absence_message(
        AbsenceTrigger.SCORECARD_WINDOW_NOT_PUBLISHED, period_label=None, other_window_ready=False
    )
    assert default["message"] == expected
    temporal = default["temporal"]
    assert temporal["version"] == 2
    assert temporal["resolved"]["window"] == {"start": "2026-08-13", "end": "2026-08-19"}
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
    month = _ok(
        _serve(
            presentation_module,
            stores,
            {"temporal_selection": {"kind": "preset", "preset_id": "last_complete_month"}},
            covered=(),
        )
    )
    assert month["reason_code"] == "no_complete_period_measured"


def _ranking_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", payload["cards"][0]["ranking"]["rows"])


def test_a_channel_with_no_spend_is_never_ranked_on_a_ratio_it_doesnt_have() -> None:
    """G9A-1: the ranking can't state a 0x for a channel the channel table says has no ROAS."""
    with _figures_patched("ad_spend", scorecard_tests.DSP, (Decimal(0), Decimal(400))):
        artifact = scorecard_tests._rendered()  # pyright: ignore[reportPrivateUsage]
    (card,) = artifact.cards
    # Source: no value is invented for the unspent channel, so no rank is concluded at all.
    assert card.ranking.outcome.value == "insufficient"
    dsp = next(row for row in card.ranking.rows if row.channel_key == scorecard_tests.DSP)
    assert {item.value for item in dsp.values} == {None}
    assert all(item.rank is None for row in card.ranking.rows for item in row.values)
    # Serializer: the served ranking names the reason, and carries no ranked value.
    payload = network_scorecard_artifact_payload(artifact)
    (served_card,) = cast("list[dict[str, Any]]", payload["cards"])
    ranking = served_card["ranking"]
    assert ranking["outcome_text"] == (
        "Not available: Amazon DSP had no ad spend in this window, so channels can't be ranked "
        "under each model."
    )
    served = [value for row in _ranking_rows(payload) for value in row["values"]]
    assert all(value["rank_text"] is None for value in served)
    assert {value["display_value"] for value in served if value["value"] == "0"} == set()
    # The channel table's reason names the missing divisor beside the revenue that shows.
    table_rows = cast("list[dict[str, Any]]", served_card["channel_table"]["rows"])
    dsp_row = next(row for row in table_rows if row["channel"] == scorecard_tests.DSP)
    assert [figure["absence_text"] for figure in dsp_row["figures"]][2:] == [
        "No ad spend on this channel in this window, so there's no spend to divide its return by."
    ] * 2
    assert dsp_row["figures"][1]["display_value"] == "1200.00"
    # Paired: with spend, the same bake ranks DSP second under every model.
    spent = network_scorecard_artifact_payload(
        scorecard_tests._rendered()  # pyright: ignore[reportPrivateUsage]
    )
    dsp_spent = next(row for row in _ranking_rows(spent) if row["channel"] == scorecard_tests.DSP)
    assert {value["rank_text"] for value in dsp_spent["values"]} == {"2nd"}


def test_two_marketplaces_serve_two_cards_and_retail_rows_that_say_which_marketplace(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """S4, S14, G9A-6: one card per marketplace and currency, and retail rows never merged."""
    with _two_marketplaces():
        stores = _Stores(_bake("7d"))
    artifact = _ok(_serve(presentation_module, stores, {}))["page"]["artifact"]
    assert [card["title"] for card in artifact["cards"]] == [
        "Amazon \u00b7 United Kingdom marketplace \u00b7 GBP",
        "Amazon \u00b7 US marketplace \u00b7 USD",
    ]
    assert [card["currency_code"] for card in artifact["cards"]] == ["GBP", "USD"]
    assert [row["marketplace_text"] for row in artifact["tacos"]] == [
        "United Kingdom marketplace",
        "US marketplace",
    ]
    spend_rows = artifact["cross_network_spend"]
    assert [(row["row_id"], row["marketplace_text"]) for row in spend_rows] == [
        ("amazon_uk-GBP", "United Kingdom marketplace \u00b7 GBP"),
        ("amazon_us-USD", "US marketplace \u00b7 USD"),
    ]
    # Never converted: every money figure on the GBP card is in GBP.
    uk = artifact["cards"][0]
    assert {
        figure["units"]["currency_code"]
        for row in uk["channel_table"]["rows"]
        for figure in row["figures"]
        if figure["format"] == "currency"
    } == {"GBP"}
    # Each insight card names its marketplace, so two cards that read alike never share a
    # name, and none offers a Campaigns step it can't scope to its marketplace (SC9A-GATE-001).
    insight_cards = artifact["insight_cards"]
    assert [(card["card_id"], card["marketplace_text"]) for card in insight_cards[:2]] == [
        ("amazon-amazon_ads-amazon_uk", "United Kingdom marketplace"),
        ("amazon-amazon_ads-amazon_us", "US marketplace"),
    ]
    assert insight_cards[0]["headline"] == insight_cards[1]["headline"]
    assert [card["title"] for card in insight_cards[:2]] == [
        "Sponsored Products ROAS rose 0.50\u00d7 \u00b7 United Kingdom marketplace",
        "Sponsored Products ROAS rose 0.50\u00d7 \u00b7 US marketplace",
    ]
    assert len({card["title"] for card in insight_cards}) == len(insight_cards)
    assert {card["next_step"] is None for card in insight_cards} == {True}
    # Each channel table serves its marketplace for the campaign-arrival tag.
    assert [card["channel_table"]["marketplace_text"] for card in artifact["cards"]] == [
        "United Kingdom marketplace",
        "US marketplace",
    ]
    # Paired: one marketplace names none, as the approved mockup draws the section, and the
    # channel mover keeps its served Campaigns step.
    single = _ok(_serve(presentation_module, _Stores(_bake("7d")), {}))["page"]["artifact"]
    assert [row["marketplace_text"] for row in single["tacos"]] == [None]
    assert [row["marketplace_text"] for row in single["cross_network_spend"]] == [None]
    assert {card["marketplace_text"] for card in single["insight_cards"]} == {None}
    assert [card["channel_table"]["marketplace_text"] for card in single["cards"]] == [None]
    assert [card["title"] for card in single["insight_cards"]] == [
        card["headline"] for card in single["insight_cards"]
    ]
    mover = next(
        card for card in single["insight_cards"] if card["finding_id"] == "channel-roas-mover"
    )
    assert mover["next_step"]["target"]["page_id"] == "campaigns"


def test_two_marketplaces_name_each_also_changed_row(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """SC9A-GATE-001: a governed row met in two marketplaces lists twice, each named."""
    spike = (Decimal(500), Decimal(400))
    with (
        _two_marketplaces(),
        _committed_alert_rules(),
        _figures_patched("ad_spend", scorecard_tests.DSP, spike),
    ):
        stores = _Stores(_bake("7d"))
    rows = _ok(_serve(presentation_module, stores, {}))["page"]["artifact"]["also_changed"]["rows"]
    assert [(row["label"], row["marketplace_text"], row["title"]) for row in rows] == [
        (
            "Amazon DSP ad spend",
            "United Kingdom marketplace",
            "Amazon DSP ad spend \u00b7 United Kingdom marketplace",
        ),
        ("Amazon DSP ad spend", "US marketplace", "Amazon DSP ad spend \u00b7 US marketplace"),
    ]
    # Paired: with one marketplace the same rule lists one row, named by its label alone.
    with _committed_alert_rules(), _figures_patched("ad_spend", scorecard_tests.DSP, spike):
        single = _Stores(_bake("7d"))
    (row,) = _ok(_serve(presentation_module, single, {}))["page"]["artifact"]["also_changed"][
        "rows"
    ]
    assert (row["marketplace_text"], row["title"]) == (None, "Amazon DSP ad spend")
    # SC9A-GATE-R4-005: with the UK's revenue at 80%, each card serves its own figures.
    with (
        _queries_patched(revenue_scale=UK_REVENUE),
        _two_marketplaces(),
        _committed_alert_rules(),
        _figures_patched("ad_spend", scorecard_tests.DSP, spike),
    ):
        varied = _Stores(_bake("7d"))
    artifact = _ok(_serve(presentation_module, varied, {}))["page"]["artifact"]
    assert [row["title"] for row in artifact["also_changed"]["rows"]] == [
        "Amazon DSP ad spend \u00b7 United Kingdom marketplace",
        "Amazon DSP ad spend \u00b7 US marketplace",
    ]
    roas = [
        [row["figures"][2]["display_value"] for row in card["channel_table"]["rows"]]
        for card in artifact["cards"]
    ]
    assert roas == [["1.92", "3.60"], ["2.40", "4.50"]]
    assert [
        card["headline"]["models"][0]["figure"]["display_value"] for card in artifact["cards"]
    ] == [
        "2.88",
        "3.60",
    ]


def _channels(card: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return the channel table's and the ranking's row channels, in served order."""
    return (
        [row["channel"] for row in card["channel_table"]["rows"]],
        [row["channel"] for row in card["ranking"]["rows"]],
    )


def test_sc1_serves_four_channel_rows_in_the_governed_order_with_its_also_changed_row(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """SC9A-GATE-R4-002: SC-1 as the default mockup draws it, from the real source and handler."""
    with _sc1():
        stores = _Stores(_bake("7d"))
    artifact = _ok(_serve(presentation_module, stores, {}))["page"]["artifact"]
    (card,) = artifact["cards"]
    # The taxonomy's order (contracts/shared/media-taxonomy.yaml), the mockup's row order.
    governed = list(FOUR_CHANNELS)
    assert _channels(card) == (governed, governed)
    assert [row["channel_label"] for row in card["channel_table"]["rows"]] == [
        "Amazon DSP",
        "Sponsored Products",
        "Sponsored Brands",
        "Sponsored Display",
    ]
    assert card["channel_table"]["absent_lines"] == [
        "No Sponsored Television delivery in this window."
    ]
    assert card["ranking"]["outcome"] == "robust_leader"
    assert [row["title"] for row in artifact["also_changed"]["rows"]] == ["Amazon DSP ad spend"]
    assert len(artifact["insight_cards"]) == 3
    assert artifact["insights"][0]["state"] == "filled"
    # SC-7: TACoS is a figure, with its change and its Retail health link.
    (tacos,) = artifact["tacos"]
    assert tacos["figure"]["value"] is not None
    assert tacos["figure"]["absence_text"] is None
    # Paired: the key order the round-4 source served puts Sponsored Products last.
    assert sorted(governed) != governed
    assert sorted(governed)[-1] == scorecard_tests.SP


def test_the_a3_visual_states_are_served_by_the_real_source(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """SC9A-GATE-R4-002: model-sensitive (SC-9), no DSP (SC-6) and no Vendor Central (SC-7b)."""
    with _sc1(), _deduplicated_patched(MODEL_SENSITIVE):
        sensitive = _Stores(_bake("7d"))
    (card,) = _ok(_serve(presentation_module, sensitive, {}))["page"]["artifact"]["cards"]
    ranking = card["ranking"]
    assert (ranking["outcome"], ranking["outcome_text"]) == (
        "model_sensitive",
        "The leading channel changes with the attribution model.",
    )
    leaders = [
        next(row["channel"] for row in ranking["rows"] if row["values"][index]["rank"] == 1)
        for index in range(3)
    ]
    assert leaders == [scorecard_tests.DSP, scorecard_tests.SP, scorecard_tests.SP]
    # Paired: SC-1's ranking agrees on one leader.
    with _sc1():
        robust = _Stores(_bake("7d"))
    (sc1,) = _ok(_serve(presentation_module, robust, {}))["page"]["artifact"]["cards"]
    assert sc1["ranking"]["outcome"] == "robust_leader"
    assert sc1["dsp_absence_text"] is None

    with (
        _queries_patched(channels=FOUR_CHANNELS[1:]),
        _committed_alert_rules(),
        _deduplicated_patched(NO_DSP_DEDUPLICATED),
    ):
        undelivered = _Stores(_bake("7d"))
    (card,) = _ok(_serve(presentation_module, undelivered, {}))["page"]["artifact"]["cards"]
    assert card["dsp_absence_text"] == "No DSP delivery in this window"
    assert scorecard_tests.DSP not in _channels(card)[0] + _channels(card)[1]
    # Each model credits the same purchases once, so the headline is one value (3.79x).
    assert {model["figure"]["display_value"] for model in card["headline"]["models"]} == {"3.79"}

    with _without_vendor_central(), _sc1():
        no_vendor = _Stores(_bake("7d"))
    (tacos,) = _ok(_serve(presentation_module, no_vendor, {}))["page"]["artifact"]["tacos"]
    assert (tacos["figure"]["value"], tacos["figure"]["absence_text"]) == (
        None,
        "Needs Vendor Central sales",
    )


def test_a_spending_channel_with_no_deduplicated_credit_names_it_and_takes_no_rank(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """Owner decision 2026-10-22: "No deduplicated credit" and no rank, not "0.00x 2nd"."""
    with _sc1(), _deduplicated_patched(NO_SB_FIRST_TOUCH_CREDIT):
        stores = _Stores(_bake("7d"))
    (card,) = _ok(_serve(presentation_module, stores, {}))["page"]["artifact"]["cards"]
    ranking = card["ranking"]
    rows = {row["channel"]: row for row in ranking["rows"]}
    first, last, linear = rows[scorecard_tests.SB]["values"]
    # The governed zero stays the cell's value; only its presentation and rank change.
    assert first == {
        "model": "first_touch_v1",
        "value": "0.0000",
        "display_value": "0.00",
        "rank": None,
        "rank_text": None,
        "credit_text": "No deduplicated credit",
    }
    assert (last["credit_text"], last["rank_text"]) == (None, "2nd")
    assert (linear["credit_text"], linear["rank_text"]) == (None, "2nd")
    # The other channels rank among themselves under first touch: 1st, 2nd and 3rd.
    assert [
        rows[channel]["values"][0]["rank_text"]
        for channel in (scorecard_tests.SP, scorecard_tests.DSP, scorecard_tests.SD)
    ] == ["1st", "2nd", "3rd"]
    assert (ranking["outcome"], ranking["outcome_text"]) == (
        "robust_leader",
        "Sponsored Products leads under all three models.",
    )
    # The headline is unchanged: each model still credits 5,290 once.
    # 5,290 / 1,500 network spend.
    assert {model["figure"]["value"] for model in card["headline"]["models"]} == {"3.5267"}
    # Paired: SC-1, where Sponsored Brands has credit, names no cell and ranks it under each.
    with _sc1():
        sc1 = _Stores(_bake("7d"))
    (base,) = _ok(_serve(presentation_module, sc1, {}))["page"]["artifact"]["cards"]
    cells = [cell for row in base["ranking"]["rows"] for cell in row["values"]]
    assert {cell["credit_text"] for cell in cells} == {None}
    assert None not in {cell["rank"] for cell in cells}


def test_a_ranking_with_no_credited_channel_serves_no_leader(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """Owner decision 2026-10-22: when no model credits any channel, nothing ranks."""
    table = scorecard_tests._DEDUPLICATED  # pyright: ignore[reportPrivateUsage]
    nothing = {model: dict.fromkeys(channels, Decimal(0)) for model, channels in table.items()}
    with _sc1(), _deduplicated_patched(nothing):
        stores = _Stores(_bake("7d"))
    (card,) = _ok(_serve(presentation_module, stores, {}))["page"]["artifact"]["cards"]
    ranking = card["ranking"]
    assert (ranking["outcome"], ranking["outcome_text"]) == (
        "insufficient",
        "Not available: no channel received deduplicated credit in this window, so channels "
        "can't be ranked under each model.",
    )
    cells = [cell for row in ranking["rows"] for cell in row["values"]]
    assert len(cells) == len(table) * len(ranking["rows"]) > len(table)
    # Every governed zero stays the cell's value, names its absence of credit and ranks nothing.
    assert {
        (cell["value"], cell["display_value"], cell["rank"], cell["rank_text"], cell["credit_text"])
        for cell in cells
    } == {("0.0000", "0.00", None, None, "No deduplicated credit")}


def browser_fixture_responses(module: _PresentationModule) -> dict[str, dict[str, Any]]:
    """Serve the Scorecard responses the browser tests decode (SC-1, SC-4, SC-11)."""
    baked = _bake("7d", "30d", "last_complete_week", "last_complete_month")
    stores = _Stores(
        baked,
        {
            ("campaigns", "7d"): CAMPAIGN_WINDOW,
            ("retail-health", "7d"): DateRange(date(2026, 9, 5), date(2026, 9, 11)),
        },
    )
    month: dict[str, object] = {
        "temporal_selection": {"kind": "preset", "preset_id": "last_complete_month"}
    }
    links = dict(stores.targets)
    # A channel with no spend in the window keeps its attributed revenue (14-day attribution
    # lags), so its two ROAS cells are absent with the server's reason (gate F1, F2, F12).
    with _figures_patched("ad_spend", scorecard_tests.DSP, (Decimal(0), Decimal(400))):
        absent_cell = _Stores(_bake("7d"), links)
    # No channel delivers in the window: the bake's outcome is `empty`.
    with _queries_patched(channels=()):
        empty = _Stores(_bake("7d"), links)
    empty_body = _body(_serve(module, empty, {}))
    # Only `July 2026` is covered and the month's bake is pending: `preset_unavailable`.
    pending = _Stores(_bake("7d"), links)
    # Two marketplaces in two currencies: one card each, never converted (S4, S14, G9A-6).
    with _two_marketplaces():
        two_marketplaces = _Stores(_bake("7d"), links)
    # DSP's spend rises 25%, meeting the committed dsp-spend-spike rule: one Also changed row,
    # with the committed alert rules as production composes the renderer (G9A-3).
    with (
        _committed_alert_rules(),
        _figures_patched("ad_spend", scorecard_tests.DSP, (Decimal(500), Decimal(400))),
    ):
        also_changed = _Stores(_bake("7d"), links)
    # Round 5 (SC9A-GATE-R4-002): SC-1, the state the approved default mockup draws, baked for
    # every preset so its window control offers all four; then the A.3 visual states.
    with _sc1():
        default = _Stores(_bake("7d", "30d", "last_complete_week", "last_complete_month"), links)
    with _sc1(), _deduplicated_patched(MODEL_SENSITIVE):
        model_sensitive = _Stores(_bake("7d"), links)
    with (
        _queries_patched(channels=FOUR_CHANNELS[1:]),
        _committed_alert_rules(),
        _deduplicated_patched(NO_DSP_DEDUPLICATED),
    ):
        no_dsp = _Stores(_bake("7d"), links)
    with _without_vendor_central(), _sc1():
        needs_vendor = _Stores(_bake("7d"), links)
    # A spending channel a model credits nothing: "No deduplicated credit", no rank (2026-10-22).
    with _sc1(), _deduplicated_patched(NO_SB_FIRST_TOUCH_CREDIT):
        no_credit = _Stores(_bake("7d"), links)
    # SC9A-GATE-R4-001, -005: two marketplaces with an Also changed row in each, and the UK's
    # revenue at 80% of the US, so a figure under the wrong marketplace would show.
    with (
        _queries_patched(revenue_scale=UK_REVENUE),
        _two_marketplaces(),
        _committed_alert_rules(),
        _figures_patched("ad_spend", scorecard_tests.DSP, DSP_SPIKE),
    ):
        two_also_changed = _Stores(_bake("7d"), links)
    return {
        "scorecard-default-v2.json": _body(_serve(module, default, {})),
        "scorecard-model-sensitive-v2.json": _body(_serve(module, model_sensitive, {})),
        "scorecard-no-dsp-v2.json": _body(_serve(module, no_dsp, {})),
        "scorecard-tacos-needs-vendor-v2.json": _body(_serve(module, needs_vendor, {})),
        "scorecard-no-deduplicated-credit-v2.json": _body(_serve(module, no_credit, {})),
        "scorecard-two-marketplaces-also-changed-v2.json": _body(
            _serve(module, two_also_changed, {})
        ),
        "scorecard-7d-v2.json": _body(_serve(module, stores, {})),
        "scorecard-month-v2.json": _body(_serve(module, stores, month)),
        "scorecard-absent-cell-v2.json": _body(_serve(module, absent_cell, {})),
        "scorecard-empty-v2.json": empty_body,
        "scorecard-two-marketplaces-v2.json": _body(_serve(module, two_marketplaces, {})),
        "scorecard-also-changed-v2.json": _body(_serve(module, also_changed, {})),
        "scorecard-pending-v2.json": _body(_serve(module, pending, month)),
        "scorecard-no-period-v2.json": _body(
            _serve(
                module, pending, month, covered=(DateRange(date(2026, 7, 5), date(2026, 8, 18)),)
            )
        ),
    }


@contextmanager
def _figures_patched(metric: str, channel: str, values: tuple[Decimal, Decimal]) -> Generator[None]:
    """Bake with one channel's fictional figure replaced, restoring the table after."""
    table = scorecard_tests._VALUES[metric]  # pyright: ignore[reportPrivateUsage]
    kept = table[channel]
    table[channel] = values
    try:
        yield
    finally:
        table[channel] = kept


@contextmanager
def _two_marketplaces() -> Generator[None]:
    """Bake with the fictional second marketplace (GBP) beside the governed US one."""
    original_queries = scorecard_tests.ScorecardQueries
    original_renderer = scorecard_tests._renderer  # pyright: ignore[reportPrivateUsage]
    markets = (scorecard_tests.UK, scorecard_tests.MARKETPLACE)
    labels = replace(
        scorecard_tests.LABELS,
        marketplaces=markets,
        marketplace_labels={
            **scorecard_tests.LABELS.marketplace_labels,
            scorecard_tests.UK: "United Kingdom",
        },
    )

    def queries(period: DateRange) -> scorecard_tests.ScorecardQueries:
        return original_queries(period, marketplaces=markets)

    def renderer(
        queries: scorecard_tests.ScorecardQueries,
    ) -> scorecard_tests.ScorecardArtifactRenderer:
        return original_renderer(queries, labels=labels)

    scorecard_tests.ScorecardQueries = queries  # type: ignore[assignment, misc]
    scorecard_tests._renderer = renderer  # pyright: ignore[reportPrivateUsage]
    try:
        yield
    finally:
        scorecard_tests.ScorecardQueries = original_queries  # type: ignore[misc]
        scorecard_tests._renderer = original_renderer  # pyright: ignore[reportPrivateUsage]


@contextmanager
def _committed_alert_rules() -> Generator[None]:
    """Bake with the committed alert rules, as production composes the renderer."""
    original = scorecard_tests._renderer  # pyright: ignore[reportPrivateUsage]
    rules = scorecard_source.scorecard_alert_rules(scorecard_tests.ROOT)

    def renderer(
        queries: scorecard_tests.ScorecardQueries,
    ) -> scorecard_tests.ScorecardArtifactRenderer:
        return replace(original(queries), alert_rules=rules)

    scorecard_tests._renderer = renderer  # pyright: ignore[reportPrivateUsage]
    try:
        yield
    finally:
        scorecard_tests._renderer = original  # pyright: ignore[reportPrivateUsage]


@contextmanager
def _queries_patched(
    *,
    channels: tuple[str, ...] | None = None,
    revenue_scale: dict[str, Decimal] | None = None,
) -> Generator[None]:
    """Bake with the fictional query fake delivering the given channels, or revenue scaled.

    Entered outside `_two_marketplaces()`, its marketplaces pass through to the same fake.
    """
    original = scorecard_tests.ScorecardQueries

    def delivering(period: DateRange, **kwargs: Any) -> scorecard_tests.ScorecardQueries:
        if channels is not None:
            kwargs["channels"] = channels
        if revenue_scale is not None:
            kwargs["revenue_scale"] = revenue_scale
        return original(period, **kwargs)

    scorecard_tests.ScorecardQueries = delivering  # type: ignore[assignment, misc]
    try:
        yield
    finally:
        scorecard_tests.ScorecardQueries = original  # type: ignore[misc]


FOUR_CHANNELS = (
    scorecard_tests.DSP,
    scorecard_tests.SP,
    scorecard_tests.SB,
    scorecard_tests.SD,
)
"""SC-1's delivering channels: every governed Amazon channel but Sponsored Television."""
DSP_SPIKE = (Decimal(500), Decimal(400))
"""DSP's spend up 25%, which meets the committed dsp-spend-spike rule (one Also changed row)."""


@contextmanager
def _sc1() -> Generator[None]:
    """Bake SC-1: four channel rows, Sponsored Television absent, one Also changed row."""
    with (
        _queries_patched(channels=FOUR_CHANNELS),
        _committed_alert_rules(),
        _figures_patched("ad_spend", scorecard_tests.DSP, DSP_SPIKE),
    ):
        yield


@contextmanager
def _deduplicated_patched(values: dict[str, dict[str, Decimal]]) -> Generator[None]:
    """Bake with some models' fictional deduplicated revenue per channel replaced."""
    table = scorecard_tests._DEDUPLICATED  # pyright: ignore[reportPrivateUsage]
    kept = {model: dict(table[model]) for model in values}
    for model, channels in values.items():
        table[model].update(channels)
    try:
        yield
    finally:
        for model, channels in kept.items():
            table[model].clear()
            table[model].update(channels)


@dataclass
class _WithoutVendorCentral(StubPublications):
    """No Vendor Central tier: the current window's TACoS answer has no publication."""

    period: DateRange | None = None

    def inspect(
        self, request: PresentationEvidenceInspectionRequest
    ) -> PresentationEvidenceInspectionResult:
        self.inspection_absent_indices = frozenset(
            index
            for index, requested in enumerate(request.plans)
            if requested.plan.metric.name == "tacos" and requested.plan.window == self.period
        )
        return super().inspect(request)


@contextmanager
def _without_vendor_central() -> Generator[None]:
    """Bake with no Vendor Central sales: TACoS reads "Needs Vendor Central sales" (SC-7b).

    Entered before any renderer patch that only passes the queries on.
    """
    original = scorecard_tests._renderer  # pyright: ignore[reportPrivateUsage]

    def renderer(
        queries: scorecard_tests.ScorecardQueries,
    ) -> scorecard_tests.ScorecardArtifactRenderer:
        return original(queries, _WithoutVendorCentral(period=queries.period))

    scorecard_tests._renderer = renderer  # pyright: ignore[reportPrivateUsage]
    try:
        yield
    finally:
        scorecard_tests._renderer = original  # pyright: ignore[reportPrivateUsage]


MODEL_SENSITIVE = {
    "first_touch_v1": {scorecard_tests.DSP: Decimal(2000), scorecard_tests.SP: Decimal(1960)}
}
"""Under first touch DSP leads; under the other two models Sponsored Products does (SC-9)."""
NO_DSP_DEDUPLICATED = {
    "last_touch_v1": {scorecard_tests.SP: Decimal(2560), scorecard_tests.SB: Decimal(900)},
    "linear_v1": {scorecard_tests.SP: Decimal(2510), scorecard_tests.SB: Decimal(900)},
}
"""With DSP out, each model credits the same three-channel total once (3,790, SC-6)."""
NO_SB_FIRST_TOUCH_CREDIT = {
    "first_touch_v1": {scorecard_tests.SB: Decimal(0), scorecard_tests.SP: Decimal(3360)}
}
"""Sponsored Brands spends but first touch credits it nothing; the model's total stays 5,290."""
UK_REVENUE = {scorecard_tests.UK: Decimal("0.8")}
"""The fictional UK marketplace returns 80% of the US revenue, so its figures differ."""


def _declared_outcomes_served(declared: tuple[str, ...], served: set[str]) -> bool:
    """Whether every outcome the page declares is one a served response carries, and back."""
    return set(declared) == served == {outcome.value for outcome in ScorecardOutcome}


def test_the_browser_fixtures_are_the_servers_own_scorecard_responses(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    responses = browser_fixture_responses(presentation_module)
    for name, served in responses.items():
        committed = json.loads((_FIXTURES / name).read_text())
        assert committed == json.loads(json.dumps(served)), name
    # SC9A-GATE-R5-001: the page declares exactly the outcomes `_serve` returns over real bakes,
    # so no declared state is drawn from a body the route can't send.
    (declared,) = next(
        page
        for page in load_v2_page_schemas(scorecard_tests.ROOT).pages
        if page.page_id == "scorecard"
    ).artifacts
    outcomes = {
        body["page"]["artifact"]["outcome"] for body in responses.values() if "page" in body
    }
    assert _declared_outcomes_served(declared.outcomes, outcomes)
    # Paired: an outcome declared beside them that no response carries fails the same check.
    assert not _declared_outcomes_served((*declared.outcomes, "insufficient"), outcomes)


def test_a_failing_scorecard_pointer_read_reaches_the_entrypoints_safe_500(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """The page error state's response, as the Lambda entrypoint answers it (SC9A-GATE-R5-001)."""
    baked = _bake("7d")
    # Paired: the same entrypoint over the same bodies serves the page when the read succeeds.
    served = serve_through_entrypoint(presentation_module, _Stores(baked), {})
    assert served["statusCode"] == 200
    assert cast("dict[str, Any]", _body(served))["page"]["artifact"]["outcome"] == "complete"
    failing = _Stores(baked, failing=frozenset({"scorecard"}))
    response = serve_through_entrypoint(presentation_module, failing, {})
    assert response["statusCode"] == 500
    assert _body(response) == {
        "code": "ADLY-UNEXPECTED-0000",
        "error": "The request could not be completed. The issue has been recorded.",
        "error_class": "invariant",
    }
    assert "scorecard" in failing.reads
