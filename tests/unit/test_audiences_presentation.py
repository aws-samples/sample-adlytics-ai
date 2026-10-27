# SPDX-License-Identifier: MIT-0
"""Public Audiences v2 route over its own attested parent and parts (ADR-141).

A part is served only when the parent custody's index lists it and its bytes match, so a reader
can never see a campaign page of another publication or one the bake never wrote.
"""

from __future__ import annotations

import json
import pathlib
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from preset_artifact_support import indexed_parent_custody
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
from test_segment_profile import (
    DSP_EXPOSED,
    MONTH,
    PERFORMANCE,
    SEPTEMBER,
    _Pointers,  # pyright: ignore[reportPrivateUsage]
    _renderer,  # pyright: ignore[reportPrivateUsage]
)

from adapters.jobs.preset_artifact_publisher.audiences_source import (
    AUDIENCES_RENDER_FORMAT,
    RetainedAudiencesPresetArtifactSource,
)
from domain.presentation.absence_copy import AbsenceTrigger, absence_message
from domain.presentation.audiences import (
    AUDIENCES_INDEX_ENTRY_MAX_BYTES,
    AUDIENCES_INDEX_SHARD_ENTRIES,
    AUDIENCES_PAGE_ROWS,
    CAMPAIGN_HISTORY_GAP_TEXT,
    AudiencesPart,
    CampaignAudiences,
    CampaignEntryState,
    CampaignIndexEntry,
    CampaignPage,
    CampaignSegment,
    audiences_campaign_index_payload,
    audiences_campaign_page_payload,
    order_campaign_rows,
    require_audiences_artifact_payload,
    require_audiences_part_payload,
    resolve_audiences_payload,
)
from domain.presentation.preset_artifacts import (
    PresetArtifactBody,
    PresetArtifactCustodyManifest,
    PresetArtifactIndexShardIdentity,
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerRosterRequest,
    PresetArtifactPointerRosterResult,
    PresetArtifactPublicationRequest,
    PresetArtifactRevision,
    PresetArtifactSiblingBody,
    PresetArtifactSiblingPair,
    PresetArtifactSiblingReadRequest,
    PresetArtifactVariant,
    canonical_preset_artifact_json,
)
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.identity import CommerceScopeKey
from domain.shared_kernel.errors import InvariantError, ValidationError
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-141")]
PATH = "/api/presentation/audiences"
PAGE = PresetArtifactPage("audiences")
VARIANT = PresetArtifactVariant("last_complete_month")
NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)
TOKENS = tuple(letter * 43 for letter in "ABCDE")
ROOT = pathlib.Path(__file__).resolve().parents[2]


def _publication() -> tuple[
    PresetArtifactPublicationRequest, tuple[PresetArtifactSiblingPair, ...]
]:
    """Bake September's DSP profile and one campaign through the real source and stream."""
    renderer, _ = _renderer(frozenset({(DSP_EXPOSED, SEPTEMBER), (PERFORMANCE, SEPTEMBER)}))
    source = RetainedAudiencesPresetArtifactSource(
        compiler=renderer,
        served=lambda: frozenset({(DSP_EXPOSED, MONTH)}),
        pointers=cast("Any", _Pointers()),
        clock=lambda: NOW,
    )
    request = source.publication_for(VARIANT)
    assert isinstance(request, PresetArtifactPublicationRequest)
    return request, tuple(request.siblings)


def _payload(outcome: str = "complete") -> dict[str, object]:
    request, _ = _publication()
    payload = cast("dict[str, object]", json.loads(request.body.payload_json))
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
    """The pointer, parent, custody and parts one publication wrote, served read-only."""

    payload: dict[str, object]
    pairs: tuple[PresetArtifactSiblingPair, ...] = ()
    reads: list[str] = field(default_factory=list[str])

    def __post_init__(self) -> None:
        revision = PresetArtifactRevision(cast("str", self.payload["revision"]))
        self.body = PresetArtifactBody(
            PAGE, VARIANT, revision, canonical_preset_artifact_json(self.payload)
        )
        self.custody, self.shards = indexed_parent_custody(
            self.body,
            {"type": "AudiencesCustody", "version": 1, "body_digest": self.body.digest},
            self.pairs,
        )
        self.pointer = PresetArtifactPointer(
            PAGE,
            VARIANT,
            revision,
            self.body.digest,
            1,
            None,
            self.custody.digest,
            resolved_window=DateRange(date(2026, 9, 1), date(2026, 9, 30)),
        )
        self.by_path = {pair[0].identity.path: pair for pair in self.pairs}

    def roster(
        self, request: PresetArtifactPointerRosterRequest
    ) -> PresetArtifactPointerRosterResult:
        self.reads.append(request.page.value)
        return PresetArtifactPointerRosterResult(request, (self.pointer,))

    def read(self, pointer: PresetArtifactPointer) -> Any:
        assert pointer == self.pointer
        self.reads.append("body")
        return self.body

    def read_sibling(self, request: PresetArtifactSiblingReadRequest) -> Any:
        identity = request.descriptor.identity
        if isinstance(identity, PresetArtifactIndexShardIdentity):
            return self.shards[identity.shard_index]
        self.reads.append(identity.path)
        body, custody = self.by_path[identity.path]
        return body if request.descriptor.body_digest == body.digest else custody


@dataclass
class _Custody:
    store: _Store

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactCustodyManifest:
        assert pointer == self.store.pointer
        return self.store.custody

    def read_sibling(self, request: PresetArtifactSiblingReadRequest) -> Any:
        identity = request.descriptor.identity
        if isinstance(identity, PresetArtifactIndexShardIdentity):
            return self.store.shards[identity.shard_index]
        return self.store.by_path[identity.path][1]


@dataclass
class _Bodies:
    store: _Store

    def roster(
        self, request: PresetArtifactPointerRosterRequest
    ) -> PresetArtifactPointerRosterResult:
        return self.store.roster(request)

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactBody:
        return cast("PresetArtifactBody", self.store.read(pointer))

    def read_sibling(self, request: PresetArtifactSiblingReadRequest) -> PresetArtifactSiblingBody:
        self.store.reads.append(request.descriptor.identity.path)
        return self.store.by_path[request.descriptor.identity.path][0]


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
    bodies = _Bodies(retained)
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
        preset_artifact_bodies=bodies,
        preset_artifact_pointers=bodies,
        preset_artifact_custody=_Custody(retained),
    )
    event = _event(PATH, {"session_id": SESSION, **(request or {})})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
    response = module.handle(event, runtime)
    return cast("dict[str, Any]", _body(response)), retained, pages


def _browser_parent() -> dict[str, object]:
    payload = resolve_audiences_payload(_payload(), NOW.date())
    payload["page_context_label"] = "Audiences · September 2026"
    payload["page_context_expires_at"] = "2099-10-16T12:00:00+00:00"
    for market in cast("list[dict[str, object]]", payload["markets"]):
        for view in cast("list[dict[str, object]]", market["views"]):
            for delivery in cast("list[dict[str, object]]", view["deliveries"]):
                for insight in cast("list[dict[str, object]]", delivery["insights"]):
                    insight["artifact_revision"] = "r" * 43
    return payload


def test_browser_fixture_is_the_servers_exact_resolved_audiences_payload() -> None:
    fixture = json.loads((ROOT / "frontend/test/fixtures/audiences-complete-v2.json").read_text())
    assert fixture == _browser_parent()


def test_browser_part_fixture_is_the_servers_exact_campaign_page() -> None:
    _, pairs = _publication()
    ((body, _custody),) = pairs
    fixture = json.loads(
        (ROOT / "frontend/test/fixtures/audiences-campaign-page-v1.json").read_text()
    )
    assert fixture == json.loads(body.payload_json)


def test_served_audiences_boundary_refuses_hostile_display_and_state_pairs() -> None:
    candidates: list[dict[str, object]] = []

    def profile_row(payload: dict[str, object]) -> dict[str, object]:
        market = cast("list[dict[str, object]]", payload["markets"])[0]
        dsp = cast("list[dict[str, object]]", market["views"])[1]
        return cast("list[dict[str, object]]", dsp["rows"])[0]

    for key, value in (
        ("segment_index_text", "99.99x"),
        ("segment_buyers_text", "999"),
    ):
        changed = deepcopy(_payload())
        profile_row(changed)[key] = value
        candidates.append(changed)
    wrong_share = deepcopy(_payload())
    cast("dict[str, object]", profile_row(wrong_share)["segment_buyer_share"])["text"] = "99.9%"
    candidates.append(wrong_share)
    wrong_state = deepcopy(_payload())
    profile_row(wrong_state)["state"] = "below_minimum"
    profile_row(wrong_state)["text"] = "Fewer than 30 buyers, so no index is shown."
    candidates.append(wrong_state)
    wrong_pages = deepcopy(_payload())
    market = cast("list[dict[str, Any]]", wrong_pages["markets"])[0]
    market["views"][1]["deliveries"][0]["page_count"] = 2
    candidates.append(wrong_pages)
    wrong_index = deepcopy(_payload())
    market = cast("list[dict[str, Any]]", wrong_index["markets"])[0]
    market["campaign_audiences"]["index"][0]["campaign_ref"] = "campaign-2"
    candidates.append(wrong_index)
    for candidate in candidates:
        with pytest.raises(ValueError):
            require_audiences_artifact_payload(candidate)
    # Control: the untouched parent is accepted.
    require_audiences_artifact_payload(_payload())


def test_public_audiences_route_reads_its_own_body_resolves_date_and_binds_context_revision(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    response, store, pages = _serve(presentation_module, _payload())
    assert response["kind"] == "page"
    assert store.reads == ["audiences", "body"]
    assert response["temporal"]["resolved"]["period_label"] == "September 2026"
    page = response["page"]
    assert (
        page["page_id"],
        page["route"],
        page["artifact_revision"],
        page["artifact_version"],
    ) == (
        "audiences",
        "/audiences/",
        pages.artifact_records[0].reference.value,
        2,
    )
    served = page["artifact"]
    assert served["revision"] == store.pointer.revision.value
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


def test_served_audiences_envelope_is_the_browser_fixture_with_its_calendar_comparison(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """The browser decodes this handler's own envelope (frontend/test/audiences-served.test.ts)."""
    response, _, _ = _serve(presentation_module, _payload())
    assert response["temporal"]["comparison"] == {
        "policy": "prior_calendar_period",
        "status": "unavailable",
        "prior_period_label": "August 2026",
    }
    served = json.loads((ROOT / "frontend/test/fixtures/audiences-served-v2.json").read_text())
    assert served == response


def test_a_campaign_page_is_served_from_the_index_with_its_parent_revision(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    _, pairs = _publication()
    store = _Store(_payload(), pairs)
    response, _, pages = _serve(
        presentation_module,
        store.payload,
        {"campaign": {"ref": "campaign-1", "page": 0}},
        store=store,
    )
    assert (response["kind"], response["parent_revision"]) == (
        "audiences_part",
        store.pointer.revision.value,
    )
    part = response["part"]
    assert (part["campaign_ref"], part["campaign_label"], part["parent_revision"]) == (
        "campaign-1",
        "Fall prospecting",
        store.pointer.revision.value,
    )
    assert part["rows"][0]["segment_cpm"]["text"] == "7.00 USD"
    # A part is one baked body: no measurement and no page record is minted for it.
    assert pages.artifact_records == []
    assert "segments/campaign-1/impressions-desc/segment-0000" in store.reads
    assert "camp-1" not in json.dumps(response)


@pytest.mark.parametrize(
    ("name", "selector"),
    [
        ("a page past the campaign's last", {"campaign": {"ref": "campaign-1", "page": 1}}),
        ("an unknown reference", {"campaign": {"ref": "campaign-2", "page": 0}}),
        ("an unserved profile page", {"profile": {"path": "dsp_exposed_only", "page": 1}}),
        ("an index shard the parent has none of", {"campaign_index": {"shard": 1}}),
    ],
)
def test_a_part_the_index_doesnt_list_is_a_validation_refusal(
    presentation_module: _PresentationModule,  # noqa: F811
    name: str,
    selector: dict[str, object],
) -> None:
    del name
    _, pairs = _publication()
    store = _Store(_payload(), pairs)
    with pytest.raises(ValidationError, match="Audiences part requested is invalid"):
        _serve(presentation_module, store.payload, selector, store=store)


@pytest.mark.parametrize(
    "selector",
    [
        {"campaign": {"ref": "campaign-0", "page": 0}},
        {"campaign": {"ref": "camp-1", "page": 0}},
        {"campaign": {"ref": "campaign-1", "page": True}},
        {"campaign": {"ref": "campaign-1", "page": -1}},
        {"campaign": {"ref": "campaign-1", "page": 10_000}},
        {"campaign": {"ref": "campaign-1"}},
        {"campaign": {"ref": "campaign-1", "page": 0, "extra": 1}},
        {"profile": {"path": "asi_everything", "page": 1}},
        {"profile": {"path": "dsp_exposed_only", "page": 0}},
        {"campaign_index": {"shard": 0}},
        {"campaign": {"ref": "campaign-1", "page": 0}, "campaign_index": {"shard": 1}},
    ],
)
def test_a_malformed_part_selector_is_refused_before_any_custody_read(
    presentation_module: _PresentationModule,  # noqa: F811
    selector: dict[str, object],
) -> None:
    _, pairs = _publication()
    store = _Store(_payload(), pairs)
    with pytest.raises(ValidationError):
        _serve(presentation_module, store.payload, selector, store=store)
    assert [read for read in store.reads if read.startswith("segments/")] == []


def test_a_listed_part_whose_bytes_disagree_is_the_malformed_refusal(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    _, pairs = _publication()
    ((body, custody),) = pairs
    tampered = json.loads(body.payload_json)
    tampered["rows"][0]["segment_impressions_text"] = "1"
    swapped = PresetArtifactSiblingBody(
        body.page,
        body.variant,
        body.revision,
        body.identity,
        canonical_preset_artifact_json(tampered),
    )
    store = _Store(_payload(), pairs)
    store.by_path[body.identity.path] = (swapped, custody)
    with pytest.raises(InvariantError, match="malformed"):
        _serve(
            presentation_module,
            store.payload,
            {"campaign": {"ref": "campaign-1", "page": 0}},
            store=store,
        )


def test_audiences_serves_no_canonical_campaign_or_marketplace_key(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    response, _, pages = _serve(presentation_module, _payload())
    served = json.dumps(response) + pages.artifact_records[0].artifact.payload_json
    assert [item for item in ("campaign_key", "camp-1", "marketplace_key") if item in served] == []
    (entry,) = response["page"]["artifact"]["markets"][0]["campaign_audiences"]["index"]
    assert (entry["campaign_ref"], entry["campaign_label"]) == ("campaign-1", "Fall prospecting")
    # Control: a body whose index carries a campaign's key is refused, never served.
    keyed = deepcopy(_payload())
    market = cast("list[dict[str, Any]]", keyed["markets"])[0]
    market["campaign_audiences"]["index"][0]["campaign_key"] = "camp-1"
    with pytest.raises(InvariantError, match="malformed"):
        _serve(presentation_module, keyed)


def test_a_parent_naming_another_revision_than_its_pointer_is_refused(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    store = _Store(_payload())
    other = deepcopy(store.payload)
    other["revision"] = "b" * 64
    stale = _Store(other)
    object.__setattr__(stale, "pointer", replace_revision(stale.pointer, store.pointer.revision))
    with pytest.raises(InvariantError, match="malformed"):
        _serve(presentation_module, other, store=stale)


def replace_revision(
    pointer: PresetArtifactPointer, revision: PresetArtifactRevision
) -> PresetArtifactPointer:
    return replace(pointer, revision=revision)


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
    # A part asked for when no month is served is refused, never a pending page.
    with pytest.raises(ValidationError, match="Audiences part requested is invalid"):
        _serve(
            presentation_module,
            store.payload,
            {"campaign": {"ref": "campaign-1", "page": 0}},
            store=store,
        )


def test_a_body_from_an_earlier_artifact_version_is_the_month_not_published_absence(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """D2 (dev 2026-10-25): the retained v1 body served HTTP 500 until a re-bake replaced it."""
    earlier = deepcopy(_payload())
    earlier["version"] = 1
    earlier["markets"] = [{"campaigns": "a v1 shape this version doesn't read"}]
    response, store, pages = _serve(presentation_module, earlier)
    assert response["kind"] == "window_unavailable"
    label = response["temporal"]["presets"][0]["period_label"]
    assert response["message"] == absence_message(
        AbsenceTrigger.AUDIENCES_MONTH_NOT_PUBLISHED, period_label=label, other_window_ready=False
    )
    assert response["reason_code"] == "preset_unavailable"
    assert response["temporal"]["presets"][0]["availability"] == "unavailable"
    assert store.reads == ["audiences", "body"]
    assert pages.artifact_records == []
    # Control: a later or non-integer version isn't superseded; it stays the malformed refusal.
    for version in (3, True, "1"):
        unknown = deepcopy(earlier)
        unknown["version"] = version
        with pytest.raises(InvariantError, match="malformed"):
            _serve(presentation_module, unknown)


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


# --- layout: bodies, pages and index shards within their bounds (design 01 §4) ---------------


def _entry(rank: int, rows: int = 1, label: str = "Campaign") -> CampaignIndexEntry:
    return CampaignIndexEntry(f"campaign-{rank}", label, rows, CampaignEntryState.AVAILABLE)


def test_index_shards_hold_100_entries_and_the_parent_carries_the_first() -> None:
    revision = "a" * 64
    for campaigns, shards in ((0, 1), (1, 1), (100, 1), (101, 2)):
        section = CampaignAudiences(True, tuple(_entry(rank) for rank in range(1, campaigns + 1)))
        assert section.index_shards == shards
    section = CampaignAudiences(True, tuple(_entry(rank) for rank in range(1, 102)))
    later = audiences_campaign_index_payload(section, 1, revision)
    assert [entry["campaign_ref"] for entry in cast("list[dict[str, Any]]", later["entries"])] == [
        "campaign-101"
    ]
    require_audiences_part_payload(later, AudiencesPart.CAMPAIGN_INDEX, revision)
    # Control: a shard read under another revision is refused.
    with pytest.raises(ValueError, match="invalid identity"):
        require_audiences_part_payload(later, AudiencesPart.CAMPAIGN_INDEX, "b" * 64)
    with pytest.raises(ValueError, match="later shards"):
        audiences_campaign_index_payload(section, 2, revision)


def test_a_wide_index_entry_and_a_full_parent_stay_within_their_registered_bytes() -> None:
    label = "N" * 120
    text = CAMPAIGN_HISTORY_GAP_TEXT.format(campaign=label, month="September 2026")
    entry = CampaignIndexEntry(
        f"campaign-{999_999}", label, 999_999, CampaignEntryState.REFUSED, text
    )
    section = CampaignAudiences(
        True,
        tuple(
            replace(entry, ref=f"campaign-{rank}")
            for rank in range(1, AUDIENCES_INDEX_SHARD_ENTRIES + 1)
        ),
    )
    shard = cast(
        "list[object]",
        audiences_campaign_index_payload(
            CampaignAudiences(True, (*section.index, replace(entry, ref="campaign-101"))),
            1,
            "a" * 64,
        )["entries"],
    )
    assert len(canonical_preset_artifact_json(cast("dict[str, object]", shard[0])).encode()) <= (
        AUDIENCES_INDEX_ENTRY_MAX_BYTES
    )
    # The parent's shard 0 at its widest, beside three paths' first pages at the row ceiling.
    first_shard = len(canonical_preset_artifact_json({"index": shard * 100}).encode())
    assert 16_000 + 3 * AUDIENCES_PAGE_ROWS * 800 + first_shard <= 256_000


def _segment(segment: str, impressions: Decimal | None) -> CampaignSegment:
    return CampaignSegment(
        "camp-1", "Campaign", segment, None, impressions, None, None, "x", None, "y"
    )


def test_campaign_rows_order_by_impressions_withheld_last_then_name_then_id() -> None:
    rows = (
        _segment("3", None),
        _segment("2", Decimal(10)),
        _segment("1", Decimal(10)),
        _segment("4", Decimal(50)),
    )
    assert [row.segment_id for row in order_campaign_rows(rows)] == ["4", "1", "2", "3"]


def test_a_withdrawn_campaigns_pages_carry_its_note_and_no_rows() -> None:
    entry = CampaignIndexEntry("campaign-1", "Campaign", 120, CampaignEntryState.WITHDRAWN, "Gone.")
    assert entry.page_count == 3
    payload = audiences_campaign_page_payload(CampaignPage(entry, 2, ()), "a" * 64)
    assert (payload["state"], payload["text"], payload["rows"], payload["paging_text"]) == (
        "withdrawn",
        "Gone.",
        [],
        None,
    )
    with pytest.raises(ValueError, match="no rows"):
        CampaignPage(entry, 0, (_segment("1", Decimal(1)),))
    # A refused campaign has no pages at all.
    refused = CampaignIndexEntry("campaign-1", "Campaign", 120, CampaignEntryState.REFUSED, "No.")
    assert refused.page_count == 0


def test_a_campaign_page_states_the_servers_paging_text() -> None:
    entry = _entry(1, 120)
    rows = tuple(_segment(str(index), Decimal(index)) for index in range(AUDIENCES_PAGE_ROWS))
    payload = audiences_campaign_page_payload(CampaignPage(entry, 1, rows), "a" * 64)
    assert (payload["page"], payload["page_count"], payload["paging_text"]) == (
        1,
        3,
        "Segments 51\u2013100 of 120",
    )
    require_audiences_part_payload(payload, AudiencesPart.CAMPAIGN, "a" * 64)
    with pytest.raises(ValueError, match="at most 50 rows"):
        CampaignPage(entry, 0, (*rows, _segment("x", Decimal(1))))


def test_the_render_format_is_4() -> None:
    assert AUDIENCES_RENDER_FORMAT == 4
