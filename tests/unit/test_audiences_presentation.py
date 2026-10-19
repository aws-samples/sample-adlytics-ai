# SPDX-License-Identifier: MIT-0
"""Public Audiences v2 route over its own attested monthly body."""

from __future__ import annotations

import json
import pathlib
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, cast

import pytest
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
from test_segment_profile import _bake_with  # pyright: ignore[reportPrivateUsage]

from domain.presentation.absence_copy import AbsenceTrigger, absence_message
from domain.presentation.audiences import (
    AUDIENCES_BODY_OVER_BOUND,
    audiences_unavailable_artifact_payload,
    require_audiences_artifact_payload,
    resolve_audiences_payload,
)
from domain.presentation.preset_artifacts import (
    PresetArtifactBody,
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerRosterRequest,
    PresetArtifactPointerRosterResult,
    PresetArtifactRevision,
    PresetArtifactVariant,
    canonical_preset_artifact_json,
)
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.identity import CommerceScopeKey
from domain.semantics.time import TimeGrain
from domain.shared_kernel.errors import InvariantError
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit]
PATH = "/api/presentation/audiences"
PAGE = PresetArtifactPage("audiences")
VARIANT = PresetArtifactVariant("last_complete_month")
NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)
TOKENS = tuple(letter * 43 for letter in "ABCDE")
ROOT = pathlib.Path(__file__).resolve().parents[2]


def _payload(outcome: str = "complete") -> dict[str, object]:
    payload = cast("dict[str, object]", _bake_with(lambda _request, result: result))
    payload["outcome"] = outcome
    market = cast("list[dict[str, object]]", payload["markets"])[0]
    market["outcome"] = outcome
    paid = cast("list[dict[str, object]]", market["views"])[0]
    paid["paid_state"] = "observed"
    paid["callout"] = None
    for delivery in cast("list[dict[str, object]]", paid["deliveries"]):
        delivery["paid_state"] = {
            "feature_observed": True,
            "period_published": False,
            "expected_after": "2026-10-16",
            "latest_available_period": {"start": "2026-08-01", "end": "2026-08-31"},
        }
    return payload


@dataclass
class _Store:
    payload: dict[str, object]
    reads: list[str] = field(default_factory=list[str])

    def __post_init__(self) -> None:
        revision = PresetArtifactRevision.from_publication_identities(
            ("audiences-fictional",), render_format=1
        )
        self.body = PresetArtifactBody(
            PAGE, VARIANT, revision, canonical_preset_artifact_json(self.payload)
        )
        self.pointer = PresetArtifactPointer(
            PAGE,
            VARIANT,
            revision,
            self.body.digest,
            1,
            None,
            None,
            resolved_window=DateRange(date(2026, 9, 1), date(2026, 9, 30)),
        )

    def roster(
        self, request: PresetArtifactPointerRosterRequest
    ) -> PresetArtifactPointerRosterResult:
        self.reads.append(request.page.value)
        return PresetArtifactPointerRosterResult(request, (self.pointer,))

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactBody:
        assert pointer == self.pointer
        self.reads.append("body")
        return self.body


def _serve(  # noqa: PLR0913 -- public route fixture plus injectable controls
    module: _PresentationModule,
    payload: dict[str, object],
    request: dict[str, object] | None = None,
    *,
    now: datetime = NOW,
    store: _Store | None = None,
) -> tuple[dict[str, Any], _Store, RecordingPages]:
    retained = store or _Store(payload)
    pages = RecordingPages(tokens=TOKENS, clock=lambda: now)
    runtime = module.runtime_factory(
        catalog=RecordingCatalog(),
        page_context=pages,
        page_artifacts=pages,
        parent_capability_issuance=pages,
        scope=AuthorizedAnalyticalScope(frozenset({CommerceScopeKey("amazon_retail_us")})),
        clock=pages.clock,
        token_minter=iter(TOKENS).__next__,
        schemas=SCHEMAS,
        metric_catalog=METRIC_CATALOG,
        preset_artifact_bodies=retained,
        preset_artifact_pointers=retained,
    )
    event = _event(PATH, {"session_id": SESSION, **(request or {})})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
    response = module.handle(event, runtime)
    return cast("dict[str, Any]", _body(response)), retained, pages


def test_browser_fixture_is_the_servers_exact_resolved_audiences_payload() -> None:
    payload = resolve_audiences_payload(_payload(), NOW.date())
    payload["page_context_label"] = "Audiences · September 2026"
    payload["page_context_expires_at"] = "2099-10-16T12:00:00+00:00"
    for market in cast("list[dict[str, object]]", payload["markets"]):
        for view in cast("list[dict[str, object]]", market["views"]):
            for delivery in cast("list[dict[str, object]]", view["deliveries"]):
                for insight in cast("list[dict[str, object]]", delivery["insights"]):
                    insight["artifact_revision"] = "r" * 43
    fixture = json.loads((ROOT / "frontend/test/fixtures/audiences-complete-v2.json").read_text())
    assert fixture == payload


def test_served_audiences_boundary_refuses_hostile_display_and_state_pairs() -> None:
    candidates: list[dict[str, object]] = []

    def parts(payload: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
        market = cast("list[dict[str, object]]", payload["markets"])[0]
        dsp = cast("list[dict[str, object]]", market["views"])[1]
        campaign = cast("dict[str, object]", market["campaign_audiences"])
        return cast("list[dict[str, object]]", dsp["rows"])[0], cast(
            "list[dict[str, object]]", campaign["rows"]
        )[0]

    wrong_share = deepcopy(_payload())
    profile, _ = parts(wrong_share)
    cast("dict[str, object]", profile["segment_buyer_share"])["text"] = "99.9%"
    candidates.append(wrong_share)

    wrong_index = deepcopy(_payload())
    profile, _ = parts(wrong_index)
    profile["segment_index_text"] = "99.99x"
    candidates.append(wrong_index)

    wrong_roas = deepcopy(_payload())
    _, campaign = parts(wrong_roas)
    campaign["segment_roas_attributed_text"] = "88.88x"
    candidates.append(wrong_roas)

    wrong_count = deepcopy(_payload())
    profile, _ = parts(wrong_count)
    profile["segment_buyers_text"] = "999"
    candidates.append(wrong_count)

    wrong_state = deepcopy(_payload())
    profile, _ = parts(wrong_state)
    profile["state"] = "below_minimum"
    profile["text"] = "Fewer than 30 buyers, so no index is shown."
    candidates.append(wrong_state)

    for candidate in candidates:
        with pytest.raises(ValueError):
            require_audiences_artifact_payload(candidate)


def test_public_audiences_route_reads_its_own_body_resolves_date_and_binds_context_revision(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    response, store, pages = _serve(presentation_module, _payload())
    assert response["kind"] == "page"
    assert store.reads == ["audiences", "body"]
    assert response["temporal"]["resolved"]["period_label"] == "September 2026"
    page = response["page"]
    assert (page["page_id"], page["route"], page["artifact_revision"]) == (
        "audiences",
        "/audiences/",
        pages.artifact_records[0].reference.value,
    )
    served = page["artifact"]
    stored = cast("dict[str, Any]", pages.artifact_records[0].artifact.payload)
    paid = served["markets"][0]["views"][0]
    assert {delivery["period_text"] for delivery in paid["deliveries"]} == {
        "Figures for September 2026 are expected after 16 Oct 2026."
    }
    revisions = {
        insight["artifact_revision"]
        for view in served["markets"][0]["views"]
        for delivery in view["deliveries"]
        for insight in delivery["insights"]
    }
    assert revisions == {page["artifact_revision"]}
    assert page["starter_questions"] == served["starter_questions"] == stored["starter_questions"]
    assert (
        page["page_context_label"] == served["page_context_label"] == stored["page_context_label"]
    )


def test_served_audiences_envelope_is_the_browser_fixture_with_its_calendar_comparison(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """The browser decodes this handler's own envelope (frontend/test/audiences-served.test.ts)."""
    response, _, _ = _serve(presentation_module, _payload())
    # A calendar comparison names its prior period and no reason code: the browser's v2 shape.
    assert response["temporal"]["comparison"] == {
        "policy": "prior_calendar_period",
        "status": "unavailable",
        "prior_period_label": "August 2026",
    }
    served = json.loads((ROOT / "frontend/test/fixtures/audiences-served-v2.json").read_text())
    assert served == response


def test_audiences_serves_no_canonical_campaign_or_marketplace_key(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    response, _, pages = _serve(presentation_module, _payload())
    served = json.dumps(response) + pages.artifact_records[0].artifact.payload_json
    # Neither the protected key fields nor their canonical values reach the body or envelope.
    assert [item for item in ("campaign_key", "camp-1", "marketplace_key") if item in served] == []
    (row,) = response["page"]["artifact"]["markets"][0]["campaign_audiences"]["rows"]
    assert (row["campaign_ref"], row["campaign_label"]) == ("campaign-1", "Fall prospecting")
    # Control: a body that carries a campaign's key is refused, never served.
    keyed = deepcopy(_payload())
    market = cast("list[dict[str, Any]]", keyed["markets"])[0]
    market["campaign_audiences"]["rows"][0]["campaign_key"] = "camp-1"
    with pytest.raises(InvariantError, match="malformed"):
        _serve(presentation_module, keyed)


def test_audiences_pending_turns_missing_after_its_own_expected_date(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    pending, _, _ = _serve(presentation_module, _payload(), now=NOW)
    missing, _, _ = _serve(
        presentation_module, _payload(), now=datetime(2026, 10, 17, 12, tzinfo=UTC)
    )
    pending_delivery = pending["page"]["artifact"]["markets"][0]["views"][0]["deliveries"][0]
    missing_delivery = missing["page"]["artifact"]["markets"][0]["views"][0]["deliveries"][0]
    assert (
        pending_delivery["period_text"]
        == "Figures for September 2026 are expected after 16 Oct 2026."
    )
    assert (
        missing_delivery["period_text"]
        == "Figures for September 2026 didn't arrive. The latest available is August 2026."
    )


@pytest.mark.parametrize("outcome", ("empty", "insufficient"))
def test_audiences_absence_bodies_serve_no_starter_fallback(
    presentation_module: _PresentationModule,  # noqa: F811
    outcome: str,
) -> None:
    response, _, pages = _serve(presentation_module, _payload(outcome))
    assert response["page"]["starter_questions"] == []
    assert response["page"]["artifact"]["starter_questions"] == []
    assert pages.artifact_records[0].artifact.payload["starter_questions"] == []


@pytest.mark.covers("ADR-139")
def test_audiences_never_appends_abs_alt_even_when_another_window_is_served(
    presentation_module: _PresentationModule,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_payload())
    object.__setattr__(store, "pointer", None)

    def always_ready(*_args: object) -> bool:
        return True

    monkeypatch.setattr(presentation_module.module, "_other_window_ready", always_ready)
    response, _, _ = _serve(presentation_module, store.payload, store=store)
    label = response["temporal"]["presets"][0]["period_label"]
    assert response["message"] == absence_message(
        AbsenceTrigger.AUDIENCES_MONTH_NOT_PUBLISHED, period_label=label, other_window_ready=False
    )


def test_audiences_missing_pointer_uses_its_one_preset_pending_contract(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    store = _Store(_payload())
    object.__setattr__(store, "pointer", None)
    response, _, pages = _serve(presentation_module, store.payload, store=store)
    assert response["kind"] == "window_unavailable"
    # ABS-AUD-1: Audiences never appends ABS-ALT.
    label = response["temporal"]["presets"][0]["period_label"]
    assert response["message"] == absence_message(
        AbsenceTrigger.AUDIENCES_MONTH_NOT_PUBLISHED, period_label=label, other_window_ready=False
    )
    assert response["reason_code"] == "preset_unavailable"
    assert [item["id"] for item in response["temporal"]["presets"]] == ["last_complete_month"]
    assert response["temporal"]["presets"][0]["availability"] == "unavailable"
    assert store.reads == ["audiences"]
    assert pages.artifact_records == []


def test_audiences_typed_over_bound_pointer_serves_pending_but_malformed_marker_refuses(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    period = CalendarPeriod(date(2026, 9, 1), date(2026, 9, 30), TimeGrain.MONTH)
    marker = audiences_unavailable_artifact_payload(period)
    response, store, pages = _serve(presentation_module, marker)
    assert (response["kind"], response["temporal"]["resolved"]["period_label"]) == (
        "window_unavailable",
        "September 2026",
    )
    assert store.reads == ["audiences", "body"]
    assert pages.artifact_records == []
    # ABS-AUD-2: the over-bound month names its detail limit and the governed DSP-campaign read.
    assert response["message"] == absence_message(
        AbsenceTrigger.AUDIENCES_MONTH_OVER_BOUND,
        period_label="September 2026",
        other_window_ready=False,
    )

    malformed = deepcopy(marker)
    malformed["reason_code"] = f"{AUDIENCES_BODY_OVER_BOUND}-unknown"
    with pytest.raises(InvariantError, match="malformed"):
        _serve(presentation_module, malformed)


def test_audiences_rejects_unknown_request_fields_before_any_store_read(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    store = _Store(_payload())
    with pytest.raises(Exception, match="presentation request fields are invalid"):
        _serve(presentation_module, store.payload, {"unexpected": True}, store=store)
    assert store.reads == []


def test_audiences_rejects_malformed_body_and_wrong_or_missing_pointer_window(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    malformed = deepcopy(_payload())
    cast("dict[str, object]", malformed["month"])["unexpected"] = True
    with pytest.raises(InvariantError):
        _serve(presentation_module, malformed)

    # Paired with the parity case "index with an over-long coefficient": a coefficient past the
    # decimal context's precision is the route's typed refusal, not an escaping InvalidOperation.
    over_long = deepcopy(_payload())
    market = cast("list[dict[str, Any]]", over_long["markets"])[0]
    dsp_row = market["views"][1]["rows"][0]
    dsp_row["segment_index"] = f"1{'0' * 26}.00"
    dsp_row["segment_index_text"] = f"{dsp_row['segment_index']}x"
    with pytest.raises(InvariantError, match="malformed"):
        _serve(presentation_module, over_long)

    for pointer_window in (DateRange(date(2026, 8, 1), date(2026, 8, 31)), None):
        store = _Store(_payload())
        object.__setattr__(store.pointer, "resolved_window", pointer_window)
        with pytest.raises(InvariantError):
            _serve(presentation_module, store.payload, store=store)
        assert store.reads == ["audiences"] if pointer_window is None else ["audiences", "body"]
