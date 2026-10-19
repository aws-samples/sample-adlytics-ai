# SPDX-License-Identifier: MIT-0
"""Public Reach and funnel v2 route over its own attested calendar body (B5)."""

from __future__ import annotations

import inspect
import json
import pathlib
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from test_presentation_handler import (
    METRIC_CATALOG,
    OWNER,
    SCHEMAS,
    SESSION,
    RecordingCatalog,
    RecordingPages,
    _body,  # pyright: ignore[reportPrivateUsage]
    _event,  # pyright: ignore[reportPrivateUsage]
    _PresentationModule,  # pyright: ignore[reportPrivateUsage]
    presentation_module,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)
from test_reach_source import MONTH, WEEK, _baked  # pyright: ignore[reportPrivateUsage]

from domain.identity.session import IdentityError
from domain.presentation.absence_copy import AbsenceTrigger, absence_message
from domain.presentation.graph_serving import PageGeneration
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
from domain.semantics.identity import CommerceScopeKey
from domain.shared_kernel.errors import ErrorCode, InvariantError, ValidationError
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit]

PATH = "/api/presentation/reach"
PAGE = PresetArtifactPage("reach")
WEEK_VARIANT = PresetArtifactVariant("last_complete_week")
NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)
TOKENS = tuple(letter * 43 for letter in "ABCDE")
ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "frontend/test/fixtures/reach-served-week-v2.json"
COARSE_MONTH_WIRE = ROOT / "frontend/test/fixtures/reach-month-coarse-wire.json"
MONTH_VARIANT = PresetArtifactVariant("last_complete_month")
V2 = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
WEEK_SELECTION: dict[str, object] = {
    "temporal_selection": {"kind": "preset", "preset_id": "last_complete_week"}
}


@dataclass
class _Store:
    """One Reach pointer (the week unless told) and its body; records every read."""

    payload: dict[str, object]
    window: DateRange | None = WEEK.window
    reads: list[str] = field(default_factory=list[str])
    body_payload: dict[str, object] | None = None
    """Bytes served for the pointer's body when they differ from the pointer's own."""
    variant: PresetArtifactVariant = WEEK_VARIANT

    def __post_init__(self) -> None:
        revision = PresetArtifactRevision.from_publication_identities(
            ("reach-fictional",), render_format=1
        )
        own = PresetArtifactBody(
            PAGE, self.variant, revision, canonical_preset_artifact_json(self.payload)
        )
        self.pointer = PresetArtifactPointer(
            PAGE, self.variant, revision, own.digest, 1, None, None, resolved_window=self.window
        )
        self.body = (
            own
            if self.body_payload is None
            else PresetArtifactBody(
                PAGE, self.variant, revision, canonical_preset_artifact_json(self.body_payload)
            )
        )

    def roster(
        self, request: PresetArtifactPointerRosterRequest
    ) -> PresetArtifactPointerRosterResult:
        self.reads.append(request.page.value)
        return PresetArtifactPointerRosterResult(
            request,
            tuple(
                self.pointer if variant == self.variant else None for variant in request.variants
            ),
        )

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactBody:
        assert pointer == self.pointer
        self.reads.append("body")
        return self.body


def _runtime(module: _PresentationModule, store: _Store, pages: RecordingPages) -> object:
    return module.runtime_factory(
        catalog=RecordingCatalog(),
        page_context=pages,
        page_artifacts=pages,
        parent_capability_issuance=pages,
        scope=AuthorizedAnalyticalScope(frozenset({CommerceScopeKey("amazon_retail_us")})),
        clock=pages.clock,
        token_minter=iter(TOKENS).__next__,
        schemas=SCHEMAS,
        metric_catalog=METRIC_CATALOG,
        preset_artifact_bodies=store,
        preset_artifact_pointers=store,
    )


def _request(
    request: dict[str, object] | None = None,
    *,
    subject: str | None = OWNER,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    event = _event(PATH, {"session_id": SESSION, **(request or {})}, subject=subject)
    event["headers"] = V2 if headers is None else headers
    return event


def _serve(
    module: _PresentationModule,
    request: dict[str, object] | None = None,
    *,
    store: _Store | None = None,
) -> tuple[dict[str, Any], _Store, RecordingPages]:
    retained = store or _Store(_baked())
    pages = RecordingPages(tokens=TOKENS, clock=lambda: NOW)
    response = module.handle(_request(request), _runtime(module, retained, pages))
    assert response["statusCode"] == 200
    return cast("dict[str, Any]", _body(response)), retained, pages


def test_reach_route_serves_its_own_week_body_and_binds_the_page_revision(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    baked = _baked()
    response, store, pages = _serve(presentation_module, WEEK_SELECTION)
    assert response["kind"] == "page"
    # One pointer read, the page's own, then that pointer's body: nothing is measured.
    assert store.reads == ["reach", "body"]
    page = response["page"]
    (record,) = pages.artifact_records
    revision = record.reference.value
    assert {key: page[key] for key in page if key != "artifact"} == {
        "actions": [],
        "artifact_id": "reach",
        "artifact_revision": revision,
        "artifact_type": "ReachAndFunnelArtifact",
        "artifact_version": 1,
        "kind": "reach",
        "page_context_expires_at": "2026-10-12T13:00:00+00:00",
        "page_context_label": f"Reach and funnel · {WEEK.label}",
        "page_id": "reach",
        "route": "/reach/",
        "starter_questions": [
            f"Which exposure group's purchaser rate was highest in {WEEK.label}, and what is "
            "it associated with?"
        ],
    }
    served = page["artifact"]
    # The body is served as baked; only the context fields and the page revision are bound in.
    expected = deepcopy(baked)
    for market in cast("list[dict[str, Any]]", expected["markets"]):
        for slot in market["insights"]:
            slot["artifact_revision"] = revision
    expected.update(
        {
            "page_context_expires_at": page["page_context_expires_at"],
            "page_context_label": page["page_context_label"],
            "starter_questions": page["starter_questions"],
        }
    )
    assert served == expected
    assert record.artifact.payload == served
    resolved = response["temporal"]["resolved"]
    assert (resolved["display_grain"], resolved["period_label"], resolved["window"]) == (
        "week",
        WEEK.label,
        {"start": "2026-09-21", "end": "2026-09-27"},
    )
    assert resolved["selection"] == {"kind": "preset", "preset_id": "last_complete_week"}
    # A calendar comparison names its prior period and no reason code (the browser's v2 shape).
    assert response["temporal"]["comparison"] == {
        "policy": "prior_calendar_period",
        "status": "unavailable",
        "prior_period_label": WEEK.previous().label,
    }
    presets = {item["id"]: item for item in response["temporal"]["presets"]}
    assert list(presets) == ["last_complete_week", "last_complete_month", "last_complete_quarter"]
    assert presets["last_complete_week"]["availability"] == "available"
    # A preset with no pointer is offered as not available yet, never with invented dates.
    assert {
        presets[key]["reason_code"] for key in ("last_complete_month", "last_complete_quarter")
    } == {"preset_unavailable"}


def test_browser_fixture_is_the_servers_exact_reach_envelope(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """The browser's served-route fixture is this handler's own response over a real bake."""
    response, _, _ = _serve(presentation_module, WEEK_SELECTION)
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == response


def test_reach_missing_pointer_serves_its_one_preset_pending_state(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    # The default preset is the month, which has no pointer here.
    response, store, pages = _serve(presentation_module)
    assert store.reads == ["reach"]
    assert pages.artifact_records == []
    assert (response["kind"], response["reason_code"]) == (
        "window_unavailable",
        "preset_unavailable",
    )
    # ABS-REACH-1 with ABS-ALT: the week preset is published (`docs/ui/page-absence-copy.md`).
    assert response["message"] == absence_message(
        AbsenceTrigger.REACH_PERIOD_NOT_PUBLISHED,
        period_label="September 2026",
        other_window_ready=True,
    )
    assert response["recovery"] == {
        "action_id": None,
        "no_self_service_reason": "wait_for_source_coverage",
    }
    resolved = response["temporal"]["resolved"]
    # 2026-10-12: the latest complete month on the request date is September 2026.
    assert (resolved["selection"], resolved["display_grain"], resolved["period_label"]) == (
        {"kind": "default", "preset_id": None},
        "month",
        "September 2026",
    )
    presets = {item["id"]: item for item in response["temporal"]["presets"]}
    assert presets["last_complete_month"]["availability"] == "unavailable"
    # Control: the week pointer still names its served period on its button.
    assert presets["last_complete_week"]["window"] == {"start": "2026-09-21", "end": "2026-09-27"}


def test_reach_route_serves_the_bakes_coarse_month_body_with_per_channel_rows(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """The serve that returned 500 ADLY-PAGE_CONTEXT-0004 live (2026-10-18), over the bake's bytes.

    The body is `canonical_preset_artifact_json` output (sort_keys, so coarse cells arrive as
    1, 10_plus, 2, 3, 4_6, 7_9), pinned byte-for-byte by `test_reach.py`; the month is the
    default preset on the request date.
    """
    wire = COARSE_MONTH_WIRE.read_text(encoding="utf-8")
    store = _Store(json.loads(wire), window=MONTH.window, variant=MONTH_VARIANT)
    assert store.body.payload_json + "\n" == wire  # the bake's own bytes, not a re-encoding
    response, store, pages = _serve(presentation_module, store=store)
    assert response["kind"] == "page"
    assert store.reads == ["reach", "body"]
    (record,) = pages.artifact_records
    served = response["page"]["artifact"]
    assert (served["preset"], served["period"]["label"]) == (
        "last_complete_month",
        "September 2026",
    )
    frequency = served["markets"][0]["frequency"]
    assert frequency["bucket_scheme"] == "coarse"
    rows = frequency["per_channel"]["rows"]
    assert [row["ad_product_key"] for row in rows] == ["sponsored_products", "amazon_dsp"]
    for row in rows:
        assert set(row["cells"]) == {"1", "2", "3", "4_6", "7_9", "10_plus"}
    assert record.artifact.payload == served
    resolved = response["temporal"]["resolved"]
    assert (resolved["display_grain"], resolved["window"]) == (
        "month",
        {"start": "2026-09-01", "end": "2026-09-30"},
    )


@pytest.mark.parametrize(
    "request_body",
    [
        {"sort": {"field": "spend", "direction": "desc"}},
        {"metric": "overlap_share"},
        {"temporal_selection": {"kind": "preset", "preset_id": "7d"}},
        {"temporal_selection": {"kind": "custom", "start": "2026-09-01", "end": "2026-09-30"}},
    ],
)
def test_reach_refuses_unknown_fields_and_undeclared_selections_before_any_store_read(
    presentation_module: _PresentationModule,  # noqa: F811
    request_body: dict[str, object],
) -> None:
    store = _Store(_baked())
    pages = RecordingPages(tokens=TOKENS, clock=lambda: NOW)
    with pytest.raises(ValidationError):
        presentation_module.handle(
            _request(request_body), _runtime(presentation_module, store, pages)
        )
    assert store.reads == []
    assert pages.artifact_records == []


def test_reach_requires_the_versioned_representation(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    store = _Store(_baked())
    pages = RecordingPages(tokens=TOKENS, clock=lambda: NOW)
    with pytest.raises(ValidationError, match="versioned representation"):
        presentation_module.handle(
            _request(headers={}), _runtime(presentation_module, store, pages)
        )
    assert store.reads == []


def _hostile_bodies() -> dict[str, dict[str, object]]:
    bodies: dict[str, dict[str, object]] = {}
    wrong_type = _baked()
    wrong_type["type"] = "AudiencesArtifact"
    bodies["type"] = wrong_type
    wrong_preset = _baked()
    wrong_preset["preset"] = "last_complete_month"
    bodies["preset"] = wrong_preset
    wrong_label = _baked()
    cast("dict[str, object]", wrong_label["period"])["label"] = "September 2026"
    bodies["label"] = wrong_label
    not_a_period = _baked()
    cast("dict[str, object]", not_a_period["period"])["end"] = "2026-09-26"
    bodies["period"] = not_a_period
    wrong_grain = _baked()
    cast("dict[str, object]", wrong_grain["period"])["grain"] = "month"
    bodies["grain"] = wrong_grain
    slot_text = _baked()
    cast("list[dict[str, object]]", slot_text["markets"])[0]["insights"] = ["not-a-slot"]
    bodies["slot"] = slot_text
    return bodies


@pytest.mark.parametrize("case", sorted(_hostile_bodies()))
def test_reach_refuses_a_body_that_is_not_its_pointers_preset_and_period(
    presentation_module: _PresentationModule,  # noqa: F811
    case: str,
) -> None:
    store = _Store(_hostile_bodies()[case])
    pages = RecordingPages(tokens=TOKENS, clock=lambda: NOW)
    with pytest.raises(InvariantError):
        presentation_module.handle(
            _request(WEEK_SELECTION), _runtime(presentation_module, store, pages)
        )
    assert pages.artifact_records == []


def _inner_contract_bodies() -> dict[str, dict[str, object]]:
    """Attested bodies that are the pointer's own, yet fail the browser's strict decoder."""

    def market(body: dict[str, object]) -> dict[str, Any]:
        return cast("list[dict[str, Any]]", body["markets"])[0]

    bodies: dict[str, dict[str, object]] = {}
    unknown_outcome = _baked()
    unknown_outcome["outcome"] = "unknown"
    bodies["outcome"] = unknown_outcome
    no_market = _baked()
    no_market["markets"] = []
    bodies["no_market"] = no_market
    two_markets = _baked()
    second = deepcopy(market(two_markets))
    second["marketplace_key"] = "second_fictional_market"
    cast("list[object]", two_markets["markets"]).append(second)
    bodies["two_markets"] = two_markets
    section_state = _baked()
    market(section_state)["overlap"]["state"] = "partial"
    bodies["section_state"] = section_state
    market_outcome = _baked()
    market(market_outcome)["outcome"] = "empty"
    bodies["market_outcome"] = market_outcome
    extra_key = _baked()
    extra_key["total"] = None
    bodies["extra_key"] = extra_key
    boolean_version = _baked()
    boolean_version["version"] = True
    bodies["boolean_version"] = boolean_version
    insight_order = _baked()
    market(insight_order)["insights"].reverse()
    bodies["insight_order"] = insight_order
    boundary = _baked()
    boundary["boundary"] = cast("list[str]", boundary["boundary"])[:2]
    bodies["boundary"] = boundary
    return bodies


@pytest.mark.parametrize("case", sorted(_inner_contract_bodies()))
def test_reach_refuses_an_attested_body_the_browser_decoder_refuses(
    presentation_module: _PresentationModule,  # noqa: F811
    case: str,
) -> None:
    """Custody is not the contract: no page record or capability for an undecodable body."""
    store = _Store(_inner_contract_bodies()[case])
    pages = RecordingPages(tokens=TOKENS, clock=lambda: NOW)
    with pytest.raises(InvariantError, match="malformed"):
        presentation_module.handle(
            _request(WEEK_SELECTION), _runtime(presentation_module, store, pages)
        )
    # The body was the pointer's own and was read; nothing was written or issued for it.
    assert store.reads == ["reach", "body"]
    assert (pages.writes, pages.records, pages.artifact_records) == ([], [], [])
    assert pages.parent_capability_requests == []


def test_reach_refuses_a_pointer_window_or_body_digest_that_disagrees(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    pages = RecordingPages(tokens=TOKENS, clock=lambda: NOW)
    shifted = _Store(_baked(), window=WEEK.previous().window)
    with pytest.raises(InvariantError):
        presentation_module.handle(
            _request(WEEK_SELECTION), _runtime(presentation_module, shifted, pages)
        )
    swapped = _Store(_baked(), body_payload={**_baked(), "outcome": "empty"})
    with pytest.raises(InvariantError):
        presentation_module.handle(
            _request(WEEK_SELECTION), _runtime(presentation_module, swapped, pages)
        )
    assert pages.artifact_records == []
    # Control: a legacy pointer with no window still serves its attested body.
    response, _, _ = _serve(
        presentation_module, WEEK_SELECTION, store=_Store(_baked(), window=None)
    )
    assert response["kind"] == "page"


def test_reach_resolves_served_generation_once_the_base_carries_it(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """B4 merge tripwire, on the D2 merge checklist (`d2-integration-prep.md` §2.3).

    This base has no served-generation primitive, so Reach serves without one, as its four
    sibling pages do. The D2 merge brings `_served_generation` and `_with_generation`; from then
    on Reach's parent read must call both, as campaigns and retail-health do, or this fails.
    The generation is resolved before the body read, so an ungranted or refresh-pending
    pointer is refused without reading its body.
    """
    namespace = vars(presentation_module.module)
    primitives = ("_served_generation", "_with_generation")
    source = inspect.getsource(namespace["_v2_reach_parent_response"])
    body_read = "bodies.read(pointer)"
    # The anchor the ordering check uses must exist, so the check can't pass vacuously.
    assert source.count(body_read) == 1
    if not any(name in namespace for name in primitives):
        assert not any(f"{name}(" in source for name in primitives)
        return
    for name in primitives:
        assert f"{name}(" in source, f"Reach's parent read must call {name} (B4)."
    assert source.index("_served_generation(") < source.index(body_read), (
        "Reach must resolve its served generation before reading the body (B4)."
    )


def test_reach_reads_only_the_authorizer_subject_and_refuses_without_it(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    store = _Store(_baked())
    pages = RecordingPages(tokens=TOKENS, clock=lambda: NOW)
    with pytest.raises(IdentityError, match="authorizer subject"):
        presentation_module.handle(
            _request(WEEK_SELECTION, subject=None), _runtime(presentation_module, store, pages)
        )
    assert store.reads == []
    assert pages.artifact_records == []
    # The Lambda entrypoint answers 401 before composing anything for an unauthorized event.
    response = presentation_module.handler(_request(WEEK_SELECTION, subject=None), object())
    assert (response["statusCode"], _body(response)) == (401, {"error": "unauthorized"})
    # The served page binds the authorizer's subject, never a body field.
    _, _, served_pages = _serve(presentation_module, WEEK_SELECTION)
    (record,) = served_pages.artifact_records
    assert record.verified_owner_id == OWNER


_D2_PARENTS = (
    "_v2_scorecard_parent_response",
    "_v2_new_customers_parent_response",
    "_v2_audiences_parent_response",
    "_v2_reach_parent_response",
    "_v2_retention_parent_response",
)


@pytest.mark.parametrize("parent", _D2_PARENTS)
def test_b4_every_d2_parent_resolves_its_generation_before_the_body_and_emits_it(
    presentation_module: _PresentationModule,  # noqa: F811
    parent: str,
) -> None:
    """B4 (S6 §2.9): as campaigns and retail-health, before `bodies.read`, then emitted."""
    source = inspect.getsource(vars(presentation_module.module)[parent])
    assert source.count("bodies.read(pointer)") == 1
    assert source.count("_served_generation(runtime, pointer)") == 1
    assert source.index("_served_generation(") < source.index("bodies.read(pointer)")
    assert "page=_with_generation(page, generation)" in source


def test_b4_an_ungranted_generation_is_refused_with_no_body_read(
    presentation_module: _PresentationModule,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The served-generation refusal precedes the body read; nothing is minted."""
    module = presentation_module.module

    def refuse(_runtime: object, _pointer: object) -> object:
        raise InvariantError(ErrorCode("PRESENTATION", 99), "ungranted generation.")

    monkeypatch.setattr(module, "_served_generation", refuse)
    store = _Store(_baked())
    pages = RecordingPages(tokens=TOKENS, clock=lambda: NOW)
    with pytest.raises(InvariantError, match="ungranted"):
        presentation_module.handle(
            _request(WEEK_SELECTION), _runtime(presentation_module, store, pages)
        )
    assert "body" not in store.reads
    assert pages.artifact_records == []


def test_b4_a_granted_generation_is_emitted_and_a_legacy_pointer_carries_none(
    presentation_module: _PresentationModule,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = "2026-10-15T08:00:00Z"

    def granted(_runtime: object, _pointer: object) -> PageGeneration:
        return PageGeneration(built_at=built)

    monkeypatch.setattr(presentation_module.module, "_served_generation", granted)
    response, _, _ = _serve(presentation_module, WEEK_SELECTION)
    assert response["page"]["generation"] == {"built_at": built}
    # Control: outside GRAPH a grant-less pointer is unlabelled, so the envelope is unchanged.
    monkeypatch.undo()
    legacy, _, _ = _serve(presentation_module, WEEK_SELECTION)
    assert "generation" not in legacy["page"]
