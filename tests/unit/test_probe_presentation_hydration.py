# SPDX-License-Identifier: MIT-0
"""Pure parts of the hydration probe's zero-delivery and decode checks (design §2.5)."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain.operations.execution_graph_release import BakeRoster
from domain.presentation.page_schemas import TemporalPresetId

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import probe_presentation_hydration as probe  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-086")]

_SLOT = {"id": "metric-focus", "state": "filled", "prompt_version": "7", "artifact_revision": "x"}


def test_the_zero_delivery_targets_are_the_catalog_pause_and_launch_campaigns() -> None:
    assert probe.zero_delivery_campaign_names() == (
        "Northstar DSP | Flight 16",
        "Northstar DSP | Flight 29",
    )


def _detail(*, roas_prior: object, ctr_current: object) -> dict[str, object]:
    """Build a detail artifact in the served shape (read live 2026-10-06), fictional values."""
    return {
        "summary": {"attributed_roas": {"value": "11.3406"}, "spend": {"value": "1762.8700"}},
        "comparison": {
            "metrics": [
                {
                    "slot": "attributed_roas",
                    "prior": None if roas_prior is None else {"value": roas_prior},
                },
                {"slot": "spend", "prior": {"value": "1576.1200"}},
            ]
        },
        "delivery": {
            "summary": [{"slot": "ctr", "value": ctr_current}],
            "comparison": {"slots": [{"slot": "ctr", "value": {"value": "0.0790"}}]},
        },
    }


def test_a_detail_metric_is_absent_when_its_current_or_prior_value_is_typed_absent() -> None:
    served = _detail(roas_prior="10.9433", ctr_current={"value": "0.0832"})
    assert not probe.detail_metric_absent(served, "attributed_roas")  # positive control
    assert not probe.detail_metric_absent(served, "spend")
    assert not probe.detail_metric_absent(served, "ctr")
    assert probe.detail_metric_absent(
        _detail(roas_prior=None, ctr_current={"value": "1"}), "attributed_roas"
    )
    assert probe.detail_metric_absent(_detail(roas_prior="1", ctr_current=None), "ctr")
    assert probe.detail_metric_absent({"summary": {}, "comparison": None}, "spend")


@pytest.mark.parametrize(
    ("status", "code", "detail_absent", "fails"),
    [
        (409, "ADLY-PRESENTATION-0002", True, False),  # the designed refusal
        (409, "ADLY-PRESENTATION-0002", False, True),  # a served value refused is a stranding
        (409, "ADLY-PRESENTATION-0001", True, True),
        (500, None, True, True),
    ],
)
def test_only_an_evidenced_not_served_refusal_is_accepted(  # noqa: PLR0913, PLR0917
    monkeypatch: pytest.MonkeyPatch, status: int, code: str | None, detail_absent: bool, fails: bool
) -> None:
    def post(*_: object) -> tuple[int, dict[str, object]]:
        return status, {"code": code}

    monkeypatch.setattr(probe, "_post", post)
    state = probe.WalkState()
    probe._focus_record(  # pyright: ignore[reportPrivateUsage]
        session=None,  # pyright: ignore[reportArgumentType]
        state=state,
        where={"page": "campaign-metric-focus", "metric": "ctr"},
        path="/api/presentation/campaign-metric-focus",
        body={},
        detail_absent=detail_absent,
    )
    assert bool(state.failures) is fails
    assert state.records[-1]["designed_refusal"] is (not fails)


def test_a_focus_body_for_the_asked_metric_with_a_full_slot_decodes() -> None:
    body = {"metric": {"id": "ctr"}, "insight": dict(_SLOT)}
    assert probe.focus_decode_findings(body, "ctr") == []


def test_a_focus_body_for_another_metric_or_a_partial_slot_is_named() -> None:
    partial = {key: value for key, value in _SLOT.items() if key != "artifact_revision"}
    assert probe.focus_decode_findings({"metric": {"id": "spend"}, "insight": partial}, "ctr") == [
        "focus answered for spend",
        "focus insight slot lacks ['artifact_revision']",
    ]


def test_a_sampled_walk_loads_the_ends_the_middle_and_every_known_issue_row() -> None:
    names: list[object] = [f"Flight {index:02d}" for index in range(63)]
    names[20] = "Northstar DSP | Flight 29"  # the launch campaign
    names[41] = "Northstar DSP | Flight 16"  # the pause campaign
    known = frozenset(probe.zero_delivery_campaign_names())
    assert probe.sample_positions(names, known) == frozenset({0, 31, 62, 20, 41})
    # Positive control: without the known-issue list the two rows would not be loaded.
    assert probe.sample_positions(names, frozenset()) == frozenset({0, 31, 62})
    assert probe.sample_positions([], known) == frozenset()
    assert probe.sample_positions(["only"], known) == frozenset({0})


def test_the_roster_page_walk_covers_every_baked_row_not_walked_in_depth() -> None:
    """D2 seam sweep finding 11: every baked (page, variant) row is walked by exactly one walk.

    The in-depth walks cover campaigns and retail-health under the probe's own preset list, so
    any other row of those pages (campaigns#30d today) must fall to the roster walk.
    """
    roster = BakeRoster.from_contract()
    presets = {item.value for item in TemporalPresetId}
    in_depth = {
        (page, preset)
        for page in ("campaigns", "retail-health")
        for preset in probe._PRESETS  # pyright: ignore[reportPrivateUsage]
    }
    expected = {
        (row.page, row.variant if row.variant in presets else None)
        for row in roster.rows
        if (row.page, row.variant) not in in_depth
    }
    requests = probe.roster_page_requests()
    assert set(requests) == expected
    # Exact count: one request per leftover row, none dropped or repeated. Today's roster has
    # 20 rows, 6 of which the in-depth walks cover.
    assert len(requests) == len(expected) == len(roster.rows) - len(in_depth) == 14
    assert ("campaigns", "30d") in requests
    assert ("retention", None) in requests  # a vintage variant is the omitted selection


def _stub_roster(monkeypatch: pytest.MonkeyPatch, rows: tuple[tuple[str, str], ...]) -> None:
    roster = SimpleNamespace(rows=tuple(SimpleNamespace(page=p, variant=v) for p, v in rows))
    monkeypatch.setattr(probe, "BakeRoster", SimpleNamespace(from_contract=lambda: roster))


def test_a_page_with_two_non_preset_variants_is_refused_not_collapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both would map to the one omitted-selection request, so one would go unwalked."""
    _stub_roster(
        monkeypatch,
        (("retention", "latest_with_current_publication"), ("retention", "prior_vintage")),
    )
    with pytest.raises(probe.ProbeError, match="more than one non-preset variant"):
        probe.roster_page_requests()


def test_a_roster_with_no_row_left_for_the_roster_walk_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_roster(monkeypatch, (("campaigns", "1d"), ("retail-health", "7d")))
    with pytest.raises(probe.ProbeError, match="no row for the roster walk"):
        probe.roster_page_requests()


@pytest.mark.parametrize(
    ("status", "body", "fails"),
    [
        (
            200,
            {
                "kind": "page",
                "page": {"artifact": {"outcome": "complete"}, "generation": {"built_at": "t"}},
            },
            False,
        ),
        (200, {"kind": "window_unavailable", "reason_code": "preset_unavailable"}, False),
        (
            200,
            {"kind": "page", "page": {"artifact": {"outcome": "complete"}, "generation": None}},
            True,
        ),
        (500, {"code": "ADLY-PAGE_CONTEXT-0004"}, True),  # live Reach month, 2026-10-18
    ],
)
def test_a_roster_page_fails_only_on_a_non_200_or_an_unbuilt_page(  # noqa: PLR0913, PLR0917
    monkeypatch: pytest.MonkeyPatch, status: int, body: dict[str, object], fails: bool
) -> None:
    sent: list[tuple[str, dict[str, object]]] = []

    def post(
        _session: object, path: str, request: dict[str, object]
    ) -> tuple[int, dict[str, object]]:
        sent.append((path, request))
        return status, body

    monkeypatch.setattr(probe, "_post", post)
    monkeypatch.setattr(
        probe,
        "roster_page_requests",
        lambda: (("reach", "last_complete_month"), ("retention", None)),
    )
    state = probe.WalkState()
    probe._walk_roster_pages(None, state, "s")  # pyright: ignore[reportPrivateUsage, reportArgumentType]
    assert bool(state.failures) is fails
    assert sent == [
        (
            "/api/presentation/reach",
            {
                "session_id": "s",
                "temporal_selection": {"kind": "preset", "preset_id": "last_complete_month"},
            },
        ),
        ("/api/presentation/retention", {"session_id": "s"}),
    ]


_HANDLER = _ROOT / "src" / "adapters" / "inbound" / "presentation_handlers" / "handler.py"
_UNROUTED = frozenset(
    {"/api/presentation/media-efficiency/scopes", "/api/presentation/media-efficiency/artifact"}
)
_WALKED_IN_DEPTH_PATHS = frozenset(
    f"/api/presentation/{page}"
    for page in (
        "campaigns",
        "campaign-detail",
        "campaign-metric-focus",
        "portfolio-metric-focus",
        "retail-health",
        "retail-metric-focus",
    )
)


def _routed_presentation_paths() -> frozenset[str]:
    """Return the routed `TELEMETRY_OPERATIONS` paths, parsed from handler.py as text."""
    source = _HANDLER.read_text(encoding="utf-8")
    paths = dict(re.findall(r'^([A-Z_]+_PATH) = "(/api/[^"]+)"$', source, re.MULTILINE))
    block = re.search(r"^TELEMETRY_OPERATIONS = \{\n(.*?)^\}$", source, re.MULTILINE | re.DOTALL)
    assert block is not None
    names = re.findall(r'^ {4}([A-Z_]+): "[^"]+",$', block.group(1), re.MULTILINE)
    assert names, "TELEMETRY_OPERATIONS did not parse"
    return frozenset(paths[name] for name in names) - _UNROUTED


def _deep_dive_path() -> str:
    source = (_ROOT / "validation" / "probe_insight_deep_dive.py").read_text(encoding="utf-8")
    found = re.search(r'^_DEEP_PATH: Final = "([^"]+)"$', source, re.MULTILINE)
    assert found is not None
    return found.group(1)


def _probed_paths(unrostered: tuple[tuple[str, str], ...]) -> frozenset[str]:
    return frozenset(
        {
            *(f"/api/presentation/{page}" for page, _ in unrostered),
            probe._RETAIL_PRODUCT_PATH,  # pyright: ignore[reportPrivateUsage]
            *_WALKED_IN_DEPTH_PATHS,
            *(f"/api/presentation/{page}" for page, _ in probe.roster_page_requests()),
            _deep_dive_path(),
        }
    )


def test_the_probes_serve_every_routed_presentation_page_once() -> None:
    """ADR-148: the premise gate needs one served request per page alarm, so none is missed."""
    source = (_ROOT / "validation" / "probe_presentation_hydration.py").read_text(encoding="utf-8")
    # The walked-in-depth literal is not an oracle by itself: each path is posted by the probe.
    for path in _WALKED_IN_DEPTH_PATHS | {probe._RETAIL_PRODUCT_PATH}:  # pyright: ignore[reportPrivateUsage]
        assert f'"{path}"' in source
    routed = _routed_presentation_paths()
    assert len(routed) == 17
    assert _probed_paths(probe.UNROSTERED_PAGES) == routed
    # Paired control: a page dropped from the unrostered list goes unprobed.
    assert _probed_paths(probe.UNROSTERED_PAGES[1:]) != routed


def _recording_post(
    sent: list[tuple[str, dict[str, object], str]], status: int, body: dict[str, object]
) -> object:
    def post(
        _session: object,
        path: str,
        request: dict[str, object],
        *,
        accept: str = probe._MEDIA_TYPE,  # pyright: ignore[reportPrivateUsage]
    ) -> tuple[int, dict[str, object]]:
        sent.append((path, request, accept))
        return status, body

    return post


def test_each_unrostered_page_gets_its_own_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Overview over its legacy representation; the rest over v2; the product child by row."""
    sent: list[tuple[str, dict[str, object], str]] = []
    served: dict[str, object] = {
        "kind": "page",
        "page": {"artifact": {"outcome": "complete"}, "page_id": "x"},
    }
    monkeypatch.setattr(probe, "_post", _recording_post(sent, 200, served))
    state = probe.WalkState()
    probe._walk_unrostered_pages(None, state, "s", "ref-1")  # pyright: ignore[reportPrivateUsage, reportArgumentType]
    v2 = "application/vnd.adlytics.presentation-v2+json"
    assert sent == [
        ("/api/presentation/overview", {"session_id": "s"}, "application/json"),
        ("/api/presentation/amazon-channel-performance", {"session_id": "s"}, v2),
        ("/api/presentation/marketplace-comparison", {"session_id": "s"}, v2),
        ("/api/presentation/data-health", {"session_id": "s"}, v2),
        (
            "/api/presentation/retail-health-product",
            {
                "selection_reference": "ref-1",
                "session_id": "s",
                "temporal_selection": {"kind": "preset", "preset_id": "7d"},
            },
            v2,
        ),
    ]
    assert state.failures == []
    assert state.parents == 5


@pytest.mark.parametrize(
    ("status", "body", "fails"),
    [
        (200, {"page": {"page_id": "decision-overview"}}, False),
        (200, {"page": {}}, True),  # a 200 without a page body is no served page
        (200, {}, True),
        (400, {"code": "ADLY-PRESENTATION-0001"}, True),  # the v2 refusal shape
        (500, {}, True),
    ],
)
def test_legacy_overview_passes_only_on_200_with_a_page_body(  # noqa: PLR0913, PLR0917
    monkeypatch: pytest.MonkeyPatch, status: int, body: dict[str, object], fails: bool
) -> None:
    sent: list[tuple[str, dict[str, object], str]] = []
    monkeypatch.setattr(probe, "_post", _recording_post(sent, status, body))
    monkeypatch.setattr(probe, "UNROSTERED_PAGES", (("overview", "legacy"),))
    state = probe.WalkState()
    probe._walk_unrostered_pages(None, state, "s", None)  # pyright: ignore[reportPrivateUsage, reportArgumentType]
    assert bool(state.failures) is fails
    assert [path for path, _, _ in sent] == ["/api/presentation/overview"]


def test_a_non_200_unrostered_page_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict[str, object], str]] = []
    monkeypatch.setattr(probe, "_post", _recording_post(sent, 500, {"code": "ADLY-X-0001"}))
    state = probe.WalkState()
    probe._walk_unrostered_pages(None, state, "s", "ref-1")  # pyright: ignore[reportPrivateUsage, reportArgumentType]
    assert {failure["page"] for failure in state.failures} == {
        "overview",
        "amazon-channel-performance",
        "marketplace-comparison",
        "data-health",
        "retail-health-product",
    }
    assert {failure["check"] for failure in state.failures} == {"non_200_response"}


@pytest.mark.parametrize(
    ("rows", "reference"),
    [
        ([{"selection_reference": None}, {"selection_reference": "ref-2"}], "ref-2"),
        ([{"selection_reference": None}], None),
        ([], None),
    ],
)
def test_the_retail_walk_returns_a_product_row_or_fails(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, object]], reference: str | None
) -> None:
    served: dict[str, object] = {
        "kind": "page",
        "page": {"artifact": {"outcome": "empty", "rows": rows}},
    }
    monkeypatch.setattr(probe, "_post", _recording_post([], 200, served))
    state = probe.WalkState()
    found = probe._walk_retail(None, state, "s")  # pyright: ignore[reportPrivateUsage, reportArgumentType]
    assert found == reference
    missing = [failure for failure in state.failures if failure["check"] == "missing_product_row"]
    assert bool(missing) is (reference is None)
