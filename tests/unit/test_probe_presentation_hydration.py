# SPDX-License-Identifier: MIT-0
"""Pure parts of the hydration probe's zero-delivery and decode checks (design §2.5)."""

from __future__ import annotations

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
