# SPDX-License-Identifier: MIT-0
"""Paired controls for the over-bound Audiences acceptance verdict (merge-gate finding M2).

`tests/integration/test_d2_audiences_over_bound_acceptance.py` runs only in D2's live-acceptance
step, so its verdict runs here on fictional read-backs: one accepted month A to month B
transition, then one refusal per closed check. The fictional route bodies carry the fields
`_audiences_window_unavailable` emits (`test_audiences_presentation.py` holds the served shape).
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from test_segment_profile import _bake_with  # pyright: ignore[reportPrivateUsage]
from tests.integration import d2_graph_acceptance as acceptance

from adapters.jobs.preset_artifact_publisher.bakeability import audiences_row_budget
from domain.operations.execution_graph_dispatch import NodeState
from domain.operations.execution_graph_release import (
    BakeEffectKind,
    ServingBinding,
    serving_generation,
)
from domain.operations.run_request import RunState
from domain.presentation.audiences import (
    AUDIENCES_ARTIFACT_TYPE,
    audiences_unavailable_artifact_payload,
)
from domain.presentation.preset_artifacts import (
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactRevision,
    PresetArtifactVariant,
)
from domain.presentation.temporal import TemporalUnavailableReason
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.time import TimeGrain
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit]

GRANT = hashlib.sha256(b"fictional-over-bound-grant").hexdigest()
OLDER = hashlib.sha256(b"fictional-month-a-grant").hexdigest()
WRITTEN_AT = "2026-10-15T12:00:00Z"
NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
MONTH_A = CalendarPeriod(date(2026, 9, 1), date(2026, 9, 30), TimeGrain.MONTH)
"""The month `_bake_with` renders: month A's body is a real, whole September Audiences body."""
MONTH_B = CalendarPeriod(date(2026, 10, 1), date(2026, 10, 31), TimeGrain.MONTH)
REVISION_A = PresetArtifactRevision.from_publication_identities(("fictional-month-a",))
REVISION_B = PresetArtifactRevision.from_publication_identities(("fictional-month-b",))


def _pointer(
    revision: PresetArtifactRevision, grant: str | None, month: CalendarPeriod
) -> PresetArtifactPointer:
    page, variant = acceptance.AUDIENCES_ROW.split("#", 1)
    return PresetArtifactPointer(
        PresetArtifactPage(page),
        PresetArtifactVariant(variant),
        revision,
        "d" * 64,
        1,
        grant_id=grant,
        serving_generation=None if grant is None else serving_generation(grant),
        written_at=None if grant is None else WRITTEN_AT,
        resolved_window=DateRange(month.start, month.end),
    )


def _binding(grant: str, generation: str | None = None) -> ServingBinding:
    return ServingBinding(
        binding_id="b" * 64,
        grant_id=grant,
        conflict_unit="u" * 64,
        served_revision=NOW,
        artifact_revision=serving_generation(grant) if generation is None else generation,
        contributor_roots=("root-flow-fictional",),
        bound_at=NOW,
    )


def _route_body(month: CalendarPeriod, kind: str = acceptance.WINDOW_UNAVAILABLE) -> Any:
    window = {"start": month.start.isoformat(), "end": month.end.isoformat()}
    return {
        "envelope_version": 2,
        "kind": kind,
        "reason_code": acceptance.PRESET_UNAVAILABLE,
        "temporal": {"resolved": {"window": window, "period_label": month.label}},
    }


class _World:
    """One accepted read-back: month A served a body, the root finalized month B's marker."""

    def __init__(self) -> None:
        self.run: Any = SimpleNamespace(
            run_id="run-fictional", state=RunState.SUCCEEDED, grant_id=GRANT
        )
        self.effect = SimpleNamespace(
            grant_id=GRANT,
            page="audiences",
            variant="last_complete_month",
            effect_kind=BakeEffectKind.WRITTEN,
            artifact_revision=REVISION_B.value,
        )
        self.nodes: list[Any] = [
            SimpleNamespace(name="bake_finalize", state=NodeState.SUCCEEDED),
            SimpleNamespace(
                name=f"bake_task:{acceptance.AUDIENCES_ROW}", state=NodeState.SUCCEEDED
            ),
        ]
        self.before: tuple[PresetArtifactPointer | None, str | None] = (
            _pointer(REVISION_A, OLDER, MONTH_A),
            json.dumps(_bake_with(lambda _request, result: result)),
        )
        self.after: tuple[PresetArtifactPointer | None, str | None] = (
            _pointer(REVISION_B, GRANT, MONTH_B),
            json.dumps(audiences_unavailable_artifact_payload(MONTH_B)),
        )
        self.bindings = {GRANT: _binding(GRANT), OLDER: _binding(OLDER)}
        self.route: tuple[int, Any] = (200, _route_body(MONTH_B))

    def failures(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        def read(node: object) -> tuple[SimpleNamespace, dict[str, object]]:
            del node
            return self.effect, {}

        monkeypatch.setattr(acceptance, "read_task_effect", read)
        verdict = acceptance.audiences_over_bound_verdict(
            acceptance.OverBoundReadBack(
                self.run,
                tuple(self.nodes),
                self.before,
                self.after,
                self.bindings.get,
                self.route,
            )
        )
        return cast("list[str]", verdict["failures"])


def test_the_pending_reason_is_the_routes_own() -> None:
    """The verdict's expected reason is the one the Audiences route's pending result carries."""
    assert TemporalUnavailableReason.PRESET_UNAVAILABLE.value == acceptance.PRESET_UNAVAILABLE


def _declared() -> dict[str, object]:
    return cast("dict[str, object]", json.loads(acceptance.OVER_BOUND_DECLARATION.read_text()))


def test_the_tracked_declared_over_bound_source_is_accepted() -> None:
    """Round 4 M1: the tracked declaration's entry, above the registered body bound, binds."""
    declared = _declared()
    assert cast("int", declared["row_floor"]) == audiences_row_budget(1) + 1
    source = cast("str", declared["over_bound_source"])
    assert acceptance.declared_over_bound_source(f" {source} ") == source


@pytest.mark.parametrize(
    "supplied",
    ["", "   ", "generated/amc-segment-buyer-profile-dsp-exposed.csv", "any nonblank text"],
)
def test_control_an_undeclared_over_bound_source_is_refused(supplied: str) -> None:
    """Blank or arbitrary text is never the declared month-B delivery."""
    with pytest.raises(ValueError, match="not the declared over-bound month-B delivery"):
        acceptance.declared_over_bound_source(supplied)


def test_control_a_declaration_at_or_below_the_body_bound_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    """A row floor the body bound admits would not force the over-bound marker."""
    declared = {**_declared(), "row_floor": audiences_row_budget(1)}
    path = tmp_path / "declaration.json"
    path.write_text(json.dumps(declared))
    with pytest.raises(ValueError, match="row floor is not above the body bound"):
        acceptance.declared_over_bound_source(cast("str", declared["over_bound_source"]), path)


def test_month_b_finalized_and_served_as_pending_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _World().failures(monkeypatch) == []


def test_control_a_failed_run_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    world.run = SimpleNamespace(run_id="run-fictional", state=RunState.FAILED, grant_id=GRANT)
    assert world.failures(monkeypatch) == [f"the run is FAILED with grant {GRANT!r}"]


def test_control_an_unfinished_audiences_task_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    world.nodes[1] = SimpleNamespace(name=world.nodes[1].name, state=NodeState.CLAIMED)
    assert world.failures(monkeypatch) == [f"{acceptance.AUDIENCES_ROW}: task nodes ['CLAIMED']"]


def test_control_an_unchanged_effect_is_not_proven(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    world.effect.effect_kind = BakeEffectKind.UNCHANGED
    assert world.failures(monkeypatch) == [
        f"{acceptance.AUDIENCES_ROW}: the task's effect is unchanged"
    ]


def test_control_month_a_without_a_full_body_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    world.before = (world.before[0], world.after[1])
    assert world.failures(monkeypatch) == ["month A's pointer did not hold a full Audiences body"]


def test_control_a_malformed_body_with_the_audiences_type_tag_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 M2: the type tag alone, or a truncated body carrying it, is not a full body."""
    whole = cast("dict[str, object]", json.loads(cast("str", _World().before[1])))
    truncated = {key: value for key, value in whole.items() if key != "markets"}
    for payload in ({"type": AUDIENCES_ARTIFACT_TYPE}, truncated):
        world = _World()
        world.before = (world.before[0], json.dumps(payload))
        assert world.failures(monkeypatch) == [
            "month A's pointer did not hold a full Audiences body"
        ]


def test_control_a_full_body_for_another_month_than_its_pointer_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Month A's decoded month must be its pointer's window, as the route requires."""
    world = _World()
    august = CalendarPeriod(date(2026, 8, 1), date(2026, 8, 31), TimeGrain.MONTH)
    world.before = (_pointer(REVISION_A, OLDER, august), world.before[1])
    assert world.failures(monkeypatch) == ["month A's body is not for its pointer's window"]


def test_control_a_grant_less_pointer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    world.after = (_pointer(REVISION_B, None, MONTH_B), world.after[1])
    assert world.failures(monkeypatch) == [
        f"{acceptance.AUDIENCES_ROW}: the finalized pointer is absent or grant-less"
    ]


def test_control_a_pointer_under_another_grant_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    world.after = (_pointer(REVISION_B, OLDER, MONTH_B), world.after[1])
    assert world.failures(monkeypatch) == [
        f"{acceptance.AUDIENCES_ROW}: the finalized pointer holds another grant"
    ]


def test_control_a_stale_month_labelled_current_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner decision 1b: month A still served (no advance, a full body) never passes."""
    world = _World()
    world.effect.artifact_revision = REVISION_A.value
    world.after = (_pointer(REVISION_A, GRANT, MONTH_A), world.before[1])
    assert world.failures(monkeypatch) == [
        f"{acceptance.AUDIENCES_ROW}: the pointer did not advance past month A",
        f"{acceptance.AUDIENCES_ROW}: month B is not after month A",
        f"{acceptance.AUDIENCES_ROW}: month B's body is not the over-bound marker",
    ]


def test_control_a_marker_for_another_month_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    november = CalendarPeriod(date(2026, 11, 1), date(2026, 11, 30), TimeGrain.MONTH)
    world.after = (world.after[0], json.dumps(audiences_unavailable_artifact_payload(november)))
    assert world.failures(monkeypatch) == [
        f"{acceptance.AUDIENCES_ROW}: the marker's month is not the pointer's window",
        "the route resolved {'start': '2026-10-01', 'end': '2026-10-31'} 'October 2026', "
        "not month B {'start': '2026-10-01', 'end': '2026-10-31'} 'November 2026'",
    ]


def test_control_an_unbound_grant_is_not_served(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    del world.bindings[GRANT]
    assert world.failures(monkeypatch) == [
        f"{acceptance.AUDIENCES_ROW}: not served (RefreshPendingError)"
    ]


def test_control_a_route_serving_a_page_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    body = _route_body(MONTH_B, "page")
    body["page"] = {"artifact": {}}
    world.route = (200, body)
    assert world.failures(monkeypatch) == [
        f"the Audiences route served 'page' ({acceptance.PRESET_UNAVAILABLE!r}), "
        f"not {acceptance.WINDOW_UNAVAILABLE} ({acceptance.PRESET_UNAVAILABLE})",
        "the Audiences route served a page body for the over-bound month",
    ]


def test_control_a_route_pending_for_another_month_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World()
    world.route = (200, _route_body(MONTH_A))
    assert world.failures(monkeypatch) == [
        "the route resolved {'start': '2026-09-01', 'end': '2026-09-30'} 'September 2026', "
        "not month B {'start': '2026-10-01', 'end': '2026-10-31'} 'October 2026'"
    ]


def test_control_a_refused_route_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    world.route = (403, {})
    assert world.failures(monkeypatch) == ["the Audiences route answered 403"]
